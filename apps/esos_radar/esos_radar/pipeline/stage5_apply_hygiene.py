"""
Stage 5: Apply hygiene filters for final Tier A+ lead list.

Input:  verified_gaps.csv (from Stage 4)
Output: tier_a_plus_leads.csv (final cleaned leads)

Filters:
- Active company status (exclude dissolved, liquidation, strike-off, etc.)
- UK jurisdiction
- Recent accounts (within 18 months)
- Holding company detection (financial test + <50 employees)
- Group deduplication (identical financials = same group)
- Name-based group deduplication (similar base name = same group)
- Address-based group deduplication (same postcode + similar name patterns)
- Investment/SPV name pattern detection

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage5_apply_hygiene \
        --input data/processed/verified_gaps.csv \
        --output data/processed/tier_a_plus_leads.csv
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from radar_intel_core.clients.ch_client import CompaniesHouseClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Statuses to exclude
EXCLUDE_STATUSES = [
    "dissolved",
    "liquidation",
    "receivership",
    "administration",
    "voluntary-arrangement",
    "insolvency-proceedings",
    "converted-closed",
    "removed",
    "proposal to strike off",
]

# UK jurisdictions to include
UK_JURISDICTIONS = [
    "england-wales",
    "england",
    "wales",
    "scotland",
    "northern-ireland",
    "united-kingdom",
]

# Maximum age of accounts (days)
MAX_ACCOUNTS_AGE_DAYS = 540  # ~18 months

# Holding company detection thresholds
HOLDING_COMPANY_MAX_EMPLOYEES = 50

# Name patterns suggesting holding/investment vehicles
HOLDING_PATTERNS = [
    r'\bholdings?\b',
    r'\binvestments?\b',
    r'\bbidco\b',
    r'\btopco\b',
    r'\bholdco\b',
    r'\bnewco\b',
    r'\bspv\b',
    r'\bopco\b',
    r'\bacquisitions?\b',
]

HOLDING_PATTERN_RE = re.compile('|'.join(HOLDING_PATTERNS), re.IGNORECASE)


def is_bad_status(status: Optional[str]) -> bool:
    """Check if company status indicates inactive/problematic."""
    if not status or pd.isna(status):
        return False
    status_lower = str(status).lower()
    return any(bad in status_lower for bad in EXCLUDE_STATUSES)


def is_uk_jurisdiction(jurisdiction: Optional[str]) -> bool:
    """Check if jurisdiction is UK."""
    if not jurisdiction or pd.isna(jurisdiction):
        return True
    jurisdiction_lower = str(jurisdiction).lower().replace(" ", "-")
    return any(uk in jurisdiction_lower for uk in UK_JURISDICTIONS)


def is_accounts_recent(accounts_date: Optional[str]) -> bool:
    """Check if accounts are within acceptable age."""
    if not accounts_date or pd.isna(accounts_date):
        return True
    try:
        date_str = str(accounts_date)[:10]
        if date_str and len(date_str) >= 10:
            acc_date = date.fromisoformat(date_str)
            cutoff = date.today() - timedelta(days=MAX_ACCOUNTS_AGE_DAYS)
            return acc_date >= cutoff
        return True
    except ValueError:
        return True


def is_likely_holding_company(row: pd.Series) -> bool:
    """Detect likely holding companies (financial test + few employees)."""
    qualification_route = row.get("qualification_route", "")
    employees = row.get("employees")
    if qualification_route != "FINANCIAL_TEST":
        return False
    if pd.isna(employees):
        return False
    return employees < HOLDING_COMPANY_MAX_EMPLOYEES


def has_holding_name_pattern(company_name: Optional[str]) -> bool:
    """Check if company name suggests a holding/investment vehicle."""
    if not company_name or pd.isna(company_name):
        return False
    return bool(HOLDING_PATTERN_RE.search(company_name))


def extract_postcode(address: Optional[str]) -> Optional[str]:
    """Extract postcode from address string."""
    if not address or pd.isna(address):
        return None
    postcode_pattern = r'([A-Z]{1,2}[0-9][0-9A-Z]?\s*[0-9][A-Z]{2})'
    match = re.search(postcode_pattern, str(address).upper())
    if match:
        return match.group(1).replace(" ", "")
    return None


def extract_base_name(company_name: Optional[str]) -> str:
    """Extract base company name for grouping."""
    if not company_name or pd.isna(company_name):
        return ""

    name = str(company_name).upper()

    suffixes = [
        r'\s+PLC$', r'\s+LLP$', r'\s+LP$', r'\s+LIMITED$', r'\s+LTD\.?$',
        r'\s+HOLDINGS?$', r'\s+INVESTMENTS?$', r'\s+GROUP$', r'\s+SERVICES?$',
        r'\s+\(UK\)$', r'\s+UK$', r'\s+\d+$', r'\s+OPCO$', r'\s+BIDCO$',
        r'\s+TOPCO$', r'\s+HOLDCO$', r'\s+NEWCO$',
    ]

    for suffix in suffixes:
        name = re.sub(suffix, '', name)

    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()

    return name


def identify_duplicate_groups(df: pd.DataFrame) -> pd.DataFrame:
    """
    Identify companies that are likely in the same corporate group.

    Method 0: Similar base name (no address required) - NEW
    Method 1: Identical turnover AND balance sheet
    Method 2: Same postcode + similar name patterns (if address available)
    Method 3: Identical employees AND same postcode (if address available)
    """
    df = df.copy()

    # Check if we have address data
    has_address = "registered_address" in df.columns

    # Extract postcodes for address-based matching (if column exists)
    if has_address:
        df["_postcode"] = df["registered_address"].apply(extract_postcode)
    else:
        df["_postcode"] = None
        logger.info("  Note: No registered_address column - address-based deduplication will be skipped")

    df["_base_name"] = df["company_name"].apply(extract_base_name)

    # Initialize group tracking
    df["duplicate_group_id"] = None
    df["is_group_primary"] = True
    df["group_reason"] = None

    group_counter = 0
    processed_indices = set()

    # =========================================================================
    # Method 0: Similar base name (no address required)
    # This catches group siblings like "Harbour Healthcare Ltd" and
    # "Harbour Healthcare Holdings Ltd" without needing API data
    # =========================================================================
    logger.info("  Method 0: Checking for similar base names...")

    # Group by base name
    base_name_counts = df["_base_name"].value_counts()
    duplicate_base_names = set(base_name_counts[base_name_counts > 1].index) - {""}

    method_0_groups = 0
    for base_name in duplicate_base_names:
        if not base_name or len(base_name) < 3:  # Skip empty/tiny names
            continue

        mask = df["_base_name"] == base_name
        indices = df[mask].index.tolist()

        if len(indices) < 2:
            continue

        group_counter += 1
        method_0_groups += 1

        # Primary = most employees, non-holding name preferred
        cluster_df = df.loc[indices].copy()
        cluster_df["_has_holding_name"] = cluster_df["company_name"].apply(has_holding_name_pattern)
        cluster_df = cluster_df.sort_values(
            ["_has_holding_name", "employees"],
            ascending=[True, False],
            na_position="last"
        )
        primary_idx = cluster_df.index[0]

        for idx in indices:
            df.loc[idx, "duplicate_group_id"] = group_counter
            df.loc[idx, "group_reason"] = "similar_base_name"
            if idx != primary_idx:
                df.loc[idx, "is_group_primary"] = False
            processed_indices.add(idx)

    if method_0_groups > 0:
        logger.info(f"    Found {method_0_groups} groups via similar base name")

    # =========================================================================
    # Method 1: Identical financials (turnover + balance sheet)
    # =========================================================================
    logger.info("  Method 1: Checking for identical financials...")

    def make_financial_key(row):
        turnover = row.get("turnover")
        balance = row.get("balance_sheet")
        if pd.isna(turnover) or pd.isna(balance):
            return None
        if turnover == 0 and balance == 0:
            return None
        t_rounded = round(float(turnover) / 1000)
        b_rounded = round(float(balance) / 1000)
        return f"fin_{t_rounded}_{b_rounded}"

    df["_financial_key"] = df.apply(make_financial_key, axis=1)

    financial_key_counts = df["_financial_key"].value_counts()
    duplicate_financial_keys = set(financial_key_counts[financial_key_counts > 1].index) - {None}

    method_1_groups = 0
    for key in duplicate_financial_keys:
        mask = df["_financial_key"] == key
        indices = df[mask].index.tolist()

        # Skip if all already processed by Method 0
        unprocessed = [idx for idx in indices if idx not in processed_indices]
        if len(unprocessed) < 2:
            continue

        group_counter += 1
        method_1_groups += 1

        group_df = df.loc[unprocessed].sort_values("employees", ascending=False, na_position="last")
        primary_idx = group_df.index[0]

        for idx in unprocessed:
            df.loc[idx, "duplicate_group_id"] = group_counter
            df.loc[idx, "group_reason"] = "identical_financials"
            if idx != primary_idx:
                df.loc[idx, "is_group_primary"] = False
            processed_indices.add(idx)

    if method_1_groups > 0:
        logger.info(f"    Found {method_1_groups} groups via identical financials")

    # =========================================================================
    # Method 2 & 3: Address-based (only if we have addresses)
    # =========================================================================
    if has_address:
        logger.info("  Method 2: Checking for same postcode + similar name...")
        logger.info("  Method 3: Checking for same postcode + same employees...")

        postcode_groups = df[df["_postcode"].notna()].groupby("_postcode")

        method_2_groups = 0
        method_3_groups = 0

        # Method 2: Same postcode + similar base name
        for postcode, group in postcode_groups:
            if len(group) < 2:
                continue

            unprocessed = [idx for idx in group.index if idx not in processed_indices]
            if len(unprocessed) < 2:
                continue

            base_names = group.loc[unprocessed, "_base_name"].tolist()
            indices = group.loc[unprocessed].index.tolist()

            name_clusters = {}
            for idx, base_name in zip(indices, base_names):
                if not base_name:
                    continue
                words = base_name.split()
                if len(words) >= 1:
                    cluster_key = " ".join(words[:2])
                    if cluster_key not in name_clusters:
                        name_clusters[cluster_key] = []
                    name_clusters[cluster_key].append(idx)

            for cluster_key, cluster_indices in name_clusters.items():
                if len(cluster_indices) < 2:
                    continue

                already_grouped = [idx for idx in cluster_indices if idx in processed_indices]
                if already_grouped:
                    continue

                group_counter += 1
                method_2_groups += 1

                cluster_df = df.loc[cluster_indices].copy()
                cluster_df["_has_holding_name"] = cluster_df["company_name"].apply(has_holding_name_pattern)
                cluster_df = cluster_df.sort_values(
                    ["_has_holding_name", "employees"],
                    ascending=[True, False],
                    na_position="last"
                )
                primary_idx = cluster_df.index[0]

                for idx in cluster_indices:
                    df.loc[idx, "duplicate_group_id"] = group_counter
                    df.loc[idx, "group_reason"] = "same_postcode_similar_name"
                    if idx != primary_idx:
                        df.loc[idx, "is_group_primary"] = False
                    processed_indices.add(idx)

        if method_2_groups > 0:
            logger.info(f"    Found {method_2_groups} groups via same postcode + similar name")

        # Method 3: Same postcode + identical employees
        for postcode, group in postcode_groups:
            if len(group) < 2:
                continue

            unprocessed = [idx for idx in group.index if idx not in processed_indices]
            if len(unprocessed) < 2:
                continue

            emp_groups = group.loc[unprocessed].groupby("employees")

            for emp_count, emp_group in emp_groups:
                if pd.isna(emp_count) or len(emp_group) < 2:
                    continue

                emp_indices = emp_group.index.tolist()

                group_counter += 1
                method_3_groups += 1

                emp_df = df.loc[emp_indices].copy()
                emp_df["_has_holding_name"] = emp_df["company_name"].apply(has_holding_name_pattern)
                emp_df = emp_df.sort_values(
                    ["_has_holding_name", "company_name"],
                    ascending=[True, True]
                )
                primary_idx = emp_df.index[0]

                for idx in emp_indices:
                    df.loc[idx, "duplicate_group_id"] = group_counter
                    df.loc[idx, "group_reason"] = "same_postcode_same_employees"
                    if idx != primary_idx:
                        df.loc[idx, "is_group_primary"] = False
                    processed_indices.add(idx)

        if method_3_groups > 0:
            logger.info(f"    Found {method_3_groups} groups via same postcode + same employees")

    # Clean up temp columns
    df = df.drop(columns=["_financial_key", "_postcode", "_base_name"])

    return df


def flag_holding_name_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Flag companies with holding/investment name patterns."""
    df = df.copy()
    df["has_holding_name"] = df["company_name"].apply(has_holding_name_pattern)
    return df


def enrich_with_profile_data(
    df: pd.DataFrame,
    client: CompaniesHouseClient,
) -> pd.DataFrame:
    """Enrich dataframe with company profile data from CH API."""
    logger.info(f"Enriching {len(df):,} companies with profile data...")

    statuses = []
    jurisdictions = []
    addresses = []
    sic_codes_list = []

    for i, (_, row) in enumerate(df.iterrows()):
        if (i + 1) % 50 == 0:
            logger.info(f"Enriched {i+1:,}/{len(df):,}")

        company_number = str(row["company_number"])
        profile = client.get_company_profile(company_number)

        statuses.append(profile.get("company_status"))
        jurisdictions.append(profile.get("jurisdiction"))

        address = profile.get("registered_office_address", {})
        address_str = ", ".join(filter(None, [
            address.get("address_line_1"),
            address.get("address_line_2"),
            address.get("locality"),
            address.get("postal_code"),
        ]))
        addresses.append(address_str)

        sic = profile.get("sic_codes", [])
        sic_codes_list.append(";".join(sic) if sic else None)

    df["company_status"] = statuses
    df["jurisdiction"] = jurisdictions
    df["registered_address"] = addresses
    df["sic_codes"] = sic_codes_list

    logger.info(f"API requests made: {client.request_count:,}")

    return df


def apply_hygiene_filters(
    input_path: Path,
    output_path: Path,
    skip_enrichment: bool = False,
    keep_holding_companies: bool = False,
    keep_group_duplicates: bool = False,
    keep_holding_names: bool = False,
) -> int:
    """Apply hygiene filters to verified gaps."""
    logger.info(f"Loading verified gaps from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    initial_count = len(df)
    logger.info(f"Loaded {initial_count:,} verified gaps")

    if initial_count == 0:
        logger.warning("No verified gaps to process")
        return 0

    # Enrich with profile data if not already present
    if not skip_enrichment and "company_status" not in df.columns:
        client = CompaniesHouseClient()
        df = enrich_with_profile_data(df, client)

    filter_stats = {}

    # =========================================================================
    # FILTER 1: Holding company detection (financial test + few employees)
    # =========================================================================
    df["is_likely_holding_company"] = df.apply(is_likely_holding_company, axis=1)
    holding_co_count = df["is_likely_holding_company"].sum()
    filter_stats["holding_company_financial"] = holding_co_count

    if holding_co_count > 0:
        logger.info(f"Detected {holding_co_count:,} likely holding companies (financial test + <50 employees):")
        holding_cos = df[df["is_likely_holding_company"]][
            ["company_number", "company_name", "employees", "turnover", "balance_sheet"]
        ]
        for _, row in holding_cos.head(5).iterrows():
            turnover_str = f"£{row['turnover']/1e6:.1f}m turnover" if pd.notna(row['turnover']) else "no turnover"
            logger.info(f"    {row['company_name']}: {row['employees']:.0f} employees, {turnover_str}")

    if not keep_holding_companies:
        df = df[~df["is_likely_holding_company"]]

    # =========================================================================
    # FILTER 2: Group deduplication
    # =========================================================================
    logger.info("Running group deduplication...")
    df = identify_duplicate_groups(df)

    total_groups = df["duplicate_group_id"].dropna().nunique()
    total_duplicates = (~df["is_group_primary"] & df["duplicate_group_id"].notna()).sum()
    filter_stats["group_duplicates"] = total_duplicates

    if total_groups > 0:
        logger.info(f"Detected {total_groups:,} duplicate groups ({total_duplicates:,} secondary companies):")

        # Count by reason
        if "group_reason" in df.columns:
            reason_counts = df[df["duplicate_group_id"].notna()].groupby("group_reason")["duplicate_group_id"].nunique()
            for reason, count in reason_counts.items():
                logger.info(f"  - {reason}: {count:,} groups")

        # Show examples
        for group_id in df["duplicate_group_id"].dropna().unique()[:5]:
            group_companies = df[df["duplicate_group_id"] == group_id][
                ["company_name", "employees", "is_group_primary", "group_reason"]
            ]
            names = group_companies["company_name"].tolist()
            reason = group_companies["group_reason"].iloc[0] if "group_reason" in group_companies.columns else "unknown"
            primary_mask = group_companies["is_group_primary"]
            primary = group_companies[primary_mask]["company_name"].iloc[0] if any(primary_mask) else names[0]
            logger.info(f"    Group {int(group_id)} ({reason}): {', '.join(names)}")
            logger.info(f"      → Keeping: {primary}")

    if not keep_group_duplicates:
        pre_dedup = len(df)
        df = df[df["is_group_primary"] | df["duplicate_group_id"].isna()]
        removed = pre_dedup - len(df)
        if removed > 0:
            logger.info(f"  Removed {removed:,} duplicate group members")

    # =========================================================================
    # FILTER 3: Flag holding name patterns
    # =========================================================================
    df = flag_holding_name_patterns(df)
    holding_name_count = df["has_holding_name"].sum()
    filter_stats["holding_name_pattern"] = holding_name_count

    if holding_name_count > 0:
        logger.info(f"Flagged {holding_name_count:,} companies with holding/investment name patterns")
        holding_names = df[df["has_holding_name"]]["company_name"].head(5).tolist()
        logger.info(f"  Examples: {', '.join(holding_names)}")

    # =========================================================================
    # EXISTING FILTERS
    # =========================================================================
    if "company_status" in df.columns:
        bad_status_mask = df["company_status"].apply(is_bad_status)
        filter_stats["bad_status"] = bad_status_mask.sum()
        df = df[~bad_status_mask]

    if "jurisdiction" in df.columns:
        non_uk_mask = ~df["jurisdiction"].apply(is_uk_jurisdiction)
        filter_stats["non_uk"] = non_uk_mask.sum()
        df = df[~non_uk_mask]

    if "accounts_date" in df.columns:
        old_accounts_mask = ~df["accounts_date"].apply(is_accounts_recent)
        filter_stats["old_accounts"] = old_accounts_mask.sum()
        df = df[~old_accounts_mask]

    # Log summary
    logger.info("=" * 50)
    logger.info("Filter summary:")
    for filter_name, count in filter_stats.items():
        logger.info(f"  - {filter_name}: {count:,}")

    final_count = len(df)
    logger.info(f"Final Tier A+ leads: {final_count:,} ({100*final_count/initial_count:.1f}% of verified gaps)")

    # Sort: non-holding names first, then by employees
    if "employees" in df.columns and "has_holding_name" in df.columns:
        df = df.sort_values(
            ["has_holding_name", "employees"],
            ascending=[True, False],
            na_position="last"
        )

    # Clean up internal columns
    columns_to_drop = ["is_likely_holding_company", "is_group_primary", "group_reason"]
    df = df.drop(columns=[c for c in columns_to_drop if c in df.columns], errors="ignore")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(df):,} Tier A+ leads to {output_path}")

    # Output summary
    if "has_holding_name" in df.columns:
        clean_leads = (~df["has_holding_name"]).sum()
        holding_leads = df["has_holding_name"].sum()
        logger.info(f"  - Clean operational leads: {clean_leads:,}")
        logger.info(f"  - Holding/investment pattern leads: {holding_leads:,}")

    return len(df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply hygiene filters for final lead list"
    )
    parser.add_argument("--input", "-i", required=True, help="Path to verified_gaps.csv")
    parser.add_argument("--output", "-o", required=True, help="Path for output CSV")
    parser.add_argument("--skip-enrichment", action="store_true", help="Skip API enrichment")
    parser.add_argument("--keep-holding-companies", action="store_true", help="Keep likely holding companies")
    parser.add_argument("--keep-group-duplicates", action="store_true", help="Keep duplicate group members")
    parser.add_argument("--keep-holding-names", action="store_true", help="Don't deprioritize holding names")

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    apply_hygiene_filters(
        input_path,
        output_path,
        skip_enrichment=args.skip_enrichment,
        keep_holding_companies=args.keep_holding_companies,
        keep_group_duplicates=args.keep_group_duplicates,
        keep_holding_names=args.keep_holding_names,
    )


if __name__ == "__main__":
    main()