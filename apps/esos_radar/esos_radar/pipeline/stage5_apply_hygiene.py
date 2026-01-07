"""
Stage 5: Apply hygiene filters for final Tier A+ lead list.

Input:  verified_gaps.csv (from Stage 4)
Output: tier_a_plus_leads.csv (final cleaned leads)

Filters:
- Active company status (exclude dissolved, liquidation, strike-off, etc.)
- UK jurisdiction
- Recent accounts (within 18 months)

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
        acc_date = date.fromisoformat(str(accounts_date)[:10])
        cutoff = date.today() - timedelta(days=MAX_ACCOUNTS_AGE_DAYS)
        return acc_date >= cutoff
    except ValueError:
        return True  # Can't parse, give benefit of doubt


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
) -> int:
    """
    Apply hygiene filters to verified gaps.
    
    Args:
        input_path: Path to verified_gaps.csv
        output_path: Path for output CSV
        skip_enrichment: If True, skip API enrichment (for testing)
    
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
    
    # Filter 1: Bad status
    if "company_status" in df.columns:
        bad_status_mask = df["company_status"].apply(is_bad_status)
        filter_stats["bad_status"] = bad_status_mask.sum()
        df = df[~bad_status_mask]
    
    # Filter 2: Non-UK jurisdiction
    if "jurisdiction" in df.columns:
        non_uk_mask = ~df["jurisdiction"].apply(is_uk_jurisdiction)
        filter_stats["non_uk"] = non_uk_mask.sum()
        df = df[~non_uk_mask]
    
    # Filter 3: Old accounts
    if "accounts_date" in df.columns:
        old_accounts_mask = ~df["accounts_date"].apply(is_accounts_recent)
        filter_stats["old_accounts"] = old_accounts_mask.sum()
        df = df[~old_accounts_mask]
    
    # Log filter impacts
    logger.info("Filter impacts:")
    for filter_name, count in filter_stats.items():
        logger.info(f"  - {filter_name}: removed {count:,}")
    
    final_count = len(df)
    logger.info(f"Final Tier A+ leads: {final_count:,} ({100*final_count/initial_count:.1f}% of verified gaps)")
    
    # Sort by employees (descending) for prioritisation
    if "employees" in df.columns:
        df = df.sort_values("employees", ascending=False)
    
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
    )


if __name__ == "__main__":
    main()
