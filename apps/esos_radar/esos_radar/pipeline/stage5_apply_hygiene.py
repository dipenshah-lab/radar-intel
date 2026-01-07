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

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage5_apply_hygiene \
        --input data/processed/verified_gaps.csv \
        --output data/processed/tier_a_plus_leads.csv
"""

from __future__ import annotations

import argparse
import logging
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
HOLDING_COMPANY_MAX_EMPLOYEES = 50  # If <50 employees but qualified via financial test, likely holding co


def is_bad_status(status: Optional[str]) -> bool:
    """Check if company status indicates inactive/problematic."""
    if not status or pd.isna(status):
        return False

    status_lower = str(status).lower()
    return any(bad in status_lower for bad in EXCLUDE_STATUSES)


def is_uk_jurisdiction(jurisdiction: Optional[str]) -> bool:
    """Check if jurisdiction is UK."""
    if not jurisdiction or pd.isna(jurisdiction):
        return True  # Assume UK if not specified

    jurisdiction_lower = str(jurisdiction).lower().replace(" ", "-")
    return any(uk in jurisdiction_lower for uk in UK_JURISDICTIONS)


def is_accounts_recent(accounts_date: Optional[str]) -> bool:
    """Check if accounts are within acceptable age."""
    if not accounts_date or pd.isna(accounts_date):
        return True  # Can't determine, give benefit of doubt

    try:
        # Handle various date formats
        date_str = str(accounts_date)[:10]
        if date_str and len(date_str) >= 10:
            acc_date = date.fromisoformat(date_str)
            cutoff = date.today() - timedelta(days=MAX_ACCOUNTS_AGE_DAYS)
            return acc_date >= cutoff
        return True
    except ValueError:
        return True  # Can't parse, give benefit of doubt


def is_likely_holding_company(row: pd.Series) -> bool:
    """
    Detect likely holding companies that aren't real ESOS targets.

    Pattern: Qualified via FINANCIAL_TEST but has very few employees.
    These are typically SPVs, holding companies, or intercompany vehicles
    that have large balance sheets but no operational activity.

    Examples from the data:
    - NIANTIC INTERNATIONAL LIMITED: 3 employees, £701m turnover
    - DEMATIC GROUP LIMITED: 4 employees, £144m turnover
    - KARAN RETAIL LTD: 39 employees, £195m turnover
    """
    qualification_route = row.get("qualification_route", "")
    employees = row.get("employees")

    # Only flag if qualified via financial test
    if qualification_route != "FINANCIAL_TEST":
        return False

    # If employees is null/missing, can't determine - keep it
    if pd.isna(employees):
        return False

    # If very few employees despite large financials, likely holding company
    return employees < HOLDING_COMPANY_MAX_EMPLOYEES


def identify_duplicate_groups(df: pd.DataFrame) -> pd.DataFrame:
    """
    Identify companies that are likely in the same corporate group.

    Pattern: Multiple companies with identical turnover AND balance sheet
    are almost certainly filing consolidated/mirrored accounts.

    Examples from the data:
    - PWC Holdco 1 Limited & PWC Newco Limited: both £28.2m turnover, £45.6m balance
    - Hamburg Bidco & Hamburg Topco: both £49.6m turnover, £26.8m balance

    Returns dataframe with 'duplicate_group_id' and 'is_group_primary' columns.
    """
    df = df.copy()

    # Create a key from turnover + balance sheet (rounded to avoid float issues)
    def make_financial_key(row):
        turnover = row.get("turnover")
        balance = row.get("balance_sheet")

        # Both must be present and non-zero for matching
        if pd.isna(turnover) or pd.isna(balance):
            return None
        if turnover == 0 and balance == 0:
            return None

        # Round to nearest 1000 to handle minor variations
        t_rounded = round(float(turnover) / 1000)
        b_rounded = round(float(balance) / 1000)

        return f"{t_rounded}_{b_rounded}"

    df["_financial_key"] = df.apply(make_financial_key, axis=1)

    # Find duplicates (same financial key appears multiple times)
    key_counts = df["_financial_key"].value_counts()
    duplicate_keys = set(key_counts[key_counts > 1].index) - {None}

    # Assign group IDs
    group_id_map = {key: i + 1 for i, key in enumerate(sorted(duplicate_keys))}

    def get_group_id(key):
        if key is None or key not in duplicate_keys:
            return None
        return group_id_map[key]

    df["duplicate_group_id"] = df["_financial_key"].apply(get_group_id)

    # Mark one company per group as primary (highest employees, or first alphabetically)
    df["is_group_primary"] = True  # Default to primary

    for group_id in df["duplicate_group_id"].dropna().unique():
        group_mask = df["duplicate_group_id"] == group_id
        group_df = df[group_mask].copy()

        # Sort by employees (desc), then company name (asc) to pick primary
        group_df = group_df.sort_values(
            ["employees", "company_name"],
            ascending=[False, True],
            na_position="last"
        )

        # First one is primary, rest are not
        primary_idx = group_df.index[0]
        secondary_indices = group_df.index[1:]

        df.loc[secondary_indices, "is_group_primary"] = False

    # Clean up temp column
    df = df.drop(columns=["_financial_key"])

    return df


def enrich_with_profile_data(
    df: pd.DataFrame,
    client: CompaniesHouseClient,
) -> pd.DataFrame:
    """
    Enrich dataframe with company profile data from CH API.

    Adds: company_status, jurisdiction, registered_office fields
    """
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
) -> int:
    """
    Apply hygiene filters to verified gaps.

    Args:
        input_path: Path to verified_gaps.csv
        output_path: Path for output CSV
        skip_enrichment: If True, skip API enrichment (for testing)
        keep_holding_companies: If True, flag but don't remove holding companies
        keep_group_duplicates: If True, flag but don't remove group duplicates

    Returns:
        Number of final leads
    """
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

    # Track filter impacts
    filter_stats = {}

    # =========================================================================
    # NEW FILTER: Holding company detection
    # =========================================================================
    df["is_likely_holding_company"] = df.apply(is_likely_holding_company, axis=1)
    holding_co_count = df["is_likely_holding_company"].sum()
    filter_stats["holding_company"] = holding_co_count

    if holding_co_count > 0:
        logger.info(f"Detected {holding_co_count:,} likely holding companies:")
        holding_cos = df[df["is_likely_holding_company"]][
            ["company_number", "company_name", "employees", "turnover", "balance_sheet"]
        ]
        for _, row in holding_cos.head(5).iterrows():
            logger.info(
                f"    {row['company_name']}: {row['employees']:.0f} employees, "
                f"£{row['turnover']/1e6:.1f}m turnover"
            )

    if not keep_holding_companies:
        df = df[~df["is_likely_holding_company"]]
    else:
        logger.info("  (Flagged but kept due to --keep-holding-companies)")

    # =========================================================================
    # NEW FILTER: Group deduplication
    # =========================================================================
    df = identify_duplicate_groups(df)

    duplicate_groups = df[df["duplicate_group_id"].notna()]["duplicate_group_id"].nunique()
    duplicate_count = (~df["is_group_primary"] & df["duplicate_group_id"].notna()).sum()
    filter_stats["group_duplicates"] = duplicate_count

    if duplicate_groups > 0:
        logger.info(f"Detected {duplicate_groups:,} duplicate groups ({duplicate_count:,} secondary companies):")
        # Show examples of each group
        for group_id in df["duplicate_group_id"].dropna().unique()[:3]:
            group_companies = df[df["duplicate_group_id"] == group_id][
                ["company_name", "employees", "turnover", "is_group_primary"]
            ]
            names = ", ".join(group_companies["company_name"].tolist())
            logger.info(f"    Group {int(group_id)}: {names}")

    if not keep_group_duplicates:
        pre_dedup = len(df)
        df = df[df["is_group_primary"] | df["duplicate_group_id"].isna()]
        logger.info(f"  Removed {pre_dedup - len(df):,} duplicate group members")
    else:
        logger.info("  (Flagged but kept due to --keep-group-duplicates)")

    # =========================================================================
    # EXISTING FILTERS
    # =========================================================================

    # Filter: Bad status
    if "company_status" in df.columns:
        bad_status_mask = df["company_status"].apply(is_bad_status)
        filter_stats["bad_status"] = bad_status_mask.sum()
        df = df[~bad_status_mask]

    # Filter: Non-UK jurisdiction
    if "jurisdiction" in df.columns:
        non_uk_mask = ~df["jurisdiction"].apply(is_uk_jurisdiction)
        filter_stats["non_uk"] = non_uk_mask.sum()
        df = df[~non_uk_mask]

    # Filter: Old accounts
    if "accounts_date" in df.columns:
        old_accounts_mask = ~df["accounts_date"].apply(is_accounts_recent)
        filter_stats["old_accounts"] = old_accounts_mask.sum()
        df = df[~old_accounts_mask]

    # Log filter impacts
    logger.info("Filter summary:")
    for filter_name, count in filter_stats.items():
        logger.info(f"  - {filter_name}: {count:,}")

    final_count = len(df)
    logger.info(f"Final Tier A+ leads: {final_count:,} ({100*final_count/initial_count:.1f}% of verified gaps)")

    # Sort by employees (descending) for prioritisation
    if "employees" in df.columns:
        df = df.sort_values("employees", ascending=False, na_position="last")

    # Clean up internal columns before output
    columns_to_drop = ["is_likely_holding_company", "is_group_primary"]
    df = df.drop(columns=[c for c in columns_to_drop if c in df.columns], errors="ignore")

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(df):,} Tier A+ leads to {output_path}")

    return len(df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply hygiene filters for final lead list"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to verified_gaps.csv",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path for output CSV",
    )
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip API enrichment (assumes status/jurisdiction already present)",
    )
    parser.add_argument(
        "--keep-holding-companies",
        action="store_true",
        help="Flag but don't remove likely holding companies",
    )
    parser.add_argument(
        "--keep-group-duplicates",
        action="store_true",
        help="Flag but don't remove duplicate group members",
    )

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
    )


if __name__ == "__main__":
    main()