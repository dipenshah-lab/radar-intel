"""
Stage 3: Find ESOS-qualified companies NOT in Phase 3 notifications.

Input:  esos_qualified.csv (from Stage 2)
        esos_phase3_notifications.xlsx (Organisation Structure tab)
Output: gap_candidates.csv (companies not covered by notifications)

Uses Organisation Structure tab which contains 50,876 company numbers
covering both Responsible Undertakings and their subsidiaries.

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage3_find_gaps \
        --input data/processed/esos_qualified.csv \
        --notifications data/raw/esos_phase3_notifications.xlsx \
        --output data/processed/gap_candidates.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from radar_intel_core.esos.notifications import load_exclusion_set


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def normalise_company_number(cn: str) -> str:
    """Normalise company number for matching."""
    if pd.isna(cn):
        return ""
    return str(cn).strip().upper()


def find_gaps(
    input_path: Path,
    notifications_path: Path,
    output_path: Path,
) -> int:
    """
    Find ESOS-qualified companies not in Phase 3 notifications.
    
    Args:
        input_path: Path to esos_qualified.csv
        notifications_path: Path to ESOS Phase 3 notifications workbook
        output_path: Path for output CSV
    
    Returns:
        Number of gap candidates found
    """
    logger.info(f"Loading qualified companies from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})
    
    initial_count = len(df)
    logger.info(f"Loaded {initial_count:,} qualified companies")
    
    # Normalise company numbers
    df["company_number_norm"] = df["company_number"].apply(normalise_company_number)
    
    # Load exclusion set
    logger.info(f"Loading exclusion set from {notifications_path}")
    exclusion_set = load_exclusion_set(notifications_path)
    logger.info(f"Exclusion set contains {len(exclusion_set):,} company numbers")
    
    # Find companies NOT in exclusion set
    df["is_notified"] = df["company_number_norm"].isin(exclusion_set)
    
    notified_count = df["is_notified"].sum()
    gap_count = (~df["is_notified"]).sum()
    
    logger.info(f"Results:")
    logger.info(f"  - Already notified: {notified_count:,} ({100*notified_count/initial_count:.1f}%)")
    logger.info(f"  - Gap candidates: {gap_count:,} ({100*gap_count/initial_count:.1f}%)")
    
    # Filter to gaps only
    gap_df = df[~df["is_notified"]].copy()
    
    # Drop helper columns
    gap_df = gap_df.drop(columns=["company_number_norm", "is_notified"])
    
    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    gap_df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(gap_df):,} gap candidates to {output_path}")
    
    return len(gap_df)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find ESOS-qualified companies not in Phase 3 notifications"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to esos_qualified.csv",
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
    
    count = find_gaps(input_path, notifications_path, output_path)
    
    if count == 0:
        logger.warning("No gap candidates found - all qualified companies already notified")


if __name__ == "__main__":
    main()
