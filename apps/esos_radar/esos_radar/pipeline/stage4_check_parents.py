"""
Stage 4: Check parent company coverage via PSC tracing.

Input:  gap_candidates.csv (from Stage 3)
        esos_phase3_notifications.xlsx
Output: verified_gaps.csv (companies whose parents haven't notified)

For each gap candidate, traces the corporate ownership chain via
Companies House PSC data. If any parent is in the notifications,
the company is covered and excluded.

This is the API-intensive stage - respects CH rate limits.

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage4_check_parents \
        --input data/processed/gap_candidates.csv \
        --notifications data/raw/esos_phase3_notifications.xlsx \
        --output data/processed/verified_gaps.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional, Set

import pandas as pd

from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.esos.notifications import load_exclusion_set


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# Rate limiting: CH allows 600 requests per 5 minutes
# We'll be conservative and add delays
REQUESTS_PER_BATCH = 100
BATCH_DELAY_SECONDS = 30


def check_parent_coverage(
    company_number: str,
    client: CompaniesHouseClient,
    exclusion_set: Set[str],
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Check if a company's parent is in the ESOS notifications.
    
    Args:
        company_number: Company to check
        client: Companies House API client
        exclusion_set: Set of notified company numbers
    
    Returns:
        Tuple of (is_covered, parent_number, parent_name)
        - is_covered: True if a parent has notified
        - parent_number: The notified parent's company number (if covered)
        - parent_name: The notified parent's name (if covered)
    """
    try:
        chain = client.trace_ultimate_parent(company_number, max_depth=5)
        
        # Check each entity in the chain (skip the first, which is the company itself)
        for entity in chain[1:]:
            parent_num = entity.get("company_number")
            
            if parent_num and parent_num.upper() in exclusion_set:
                return True, parent_num, entity.get("company_name")
        
        return False, None, None
        
    except Exception as e:
        logger.warning(f"Error tracing parent for {company_number}: {e}")
        return False, None, None


def check_all_parents(
    input_path: Path,
    notifications_path: Path,
    output_path: Path,
    skip_parent_check: bool = False,
) -> int:
    """
    Check parent coverage for all gap candidates.
    
    Args:
        input_path: Path to gap_candidates.csv
        notifications_path: Path to ESOS notifications workbook
        output_path: Path for output CSV
        skip_parent_check: If True, skip API calls (for testing)
    
    Returns:
        Number of verified gaps
    """
    logger.info(f"Loading gap candidates from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})
    
    initial_count = len(df)
    logger.info(f"Loaded {initial_count:,} gap candidates")
    
    if initial_count == 0:
        logger.warning("No gap candidates to process")
        return 0
    
    # Load exclusion set
    logger.info(f"Loading exclusion set from {notifications_path}")
    exclusion_set = load_exclusion_set(notifications_path)
    # Ensure all numbers are uppercase for matching
    exclusion_set = {cn.upper() for cn in exclusion_set}
    logger.info(f"Exclusion set contains {len(exclusion_set):,} company numbers")
    
    if skip_parent_check:
        logger.info("Skipping parent check (--skip-parent-check flag)")
        df["parent_covered"] = False
        df["covering_parent_number"] = None
        df["covering_parent_name"] = None
    else:
        client = CompaniesHouseClient()
        
        parent_covered = []
        covering_parent_numbers = []
        covering_parent_names = []
        
        logger.info(f"Checking parent coverage for {initial_count:,} companies...")
        logger.info(f"Rate limiting: {REQUESTS_PER_BATCH} requests, then {BATCH_DELAY_SECONDS}s pause")
        
        for i, (_, row) in enumerate(df.iterrows()):
            company_number = str(row["company_number"])
            
            # Progress logging
            if (i + 1) % 50 == 0:
                logger.info(f"Processed {i+1:,}/{initial_count:,} companies")
            
            # Rate limiting
            if (i + 1) % REQUESTS_PER_BATCH == 0 and i > 0:
                logger.info(f"Rate limit pause: waiting {BATCH_DELAY_SECONDS}s...")
                time.sleep(BATCH_DELAY_SECONDS)
            
            is_covered, parent_num, parent_name = check_parent_coverage(
                company_number, client, exclusion_set
            )
            
            parent_covered.append(is_covered)
            covering_parent_numbers.append(parent_num)
            covering_parent_names.append(parent_name)
        
        df["parent_covered"] = parent_covered
        df["covering_parent_number"] = covering_parent_numbers
        df["covering_parent_name"] = covering_parent_names
        
        logger.info(f"API requests made: {client.request_count:,}")
    
    # Log results
    covered_count = df["parent_covered"].sum()
    verified_count = (~df["parent_covered"]).sum()
    
    logger.info(f"Results:")
    logger.info(f"  - Covered by parent notification: {covered_count:,} ({100*covered_count/initial_count:.1f}%)")
    logger.info(f"  - Verified gaps: {verified_count:,} ({100*verified_count/initial_count:.1f}%)")
    
    # Filter to verified gaps only
    verified_df = df[~df["parent_covered"]].copy()
    
    # Drop helper columns (keep parent info for reference)
    verified_df = verified_df.drop(columns=["parent_covered"])
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    verified_df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(verified_df):,} verified gaps to {output_path}")
    
    return len(verified_df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check parent company coverage via PSC tracing"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to gap_candidates.csv",
    )
    parser.add_argument(
        "--notifications", "-n",
        required=True,
        help="Path to ESOS Phase 3 notifications workbook",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path for output CSV",
    )
    parser.add_argument(
        "--skip-parent-check",
        action="store_true",
        help="Skip API calls for parent tracing (for testing)",
    )
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    notifications_path = Path(args.notifications)
    output_path = Path(args.output)
    
    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)
    
    if not notifications_path.exists():
        logger.error(f"Notifications workbook not found: {notifications_path}")
        sys.exit(1)
    
    check_all_parents(
        input_path,
        notifications_path,
        output_path,
        skip_parent_check=args.skip_parent_check,
    )


if __name__ == "__main__":
    main()
