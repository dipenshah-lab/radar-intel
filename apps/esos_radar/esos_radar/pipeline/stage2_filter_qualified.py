"""
Stage 2: Filter to companies meeting ESOS qualification thresholds.

Input:  xbrl_extracted.csv (from Stage 1)
Output: esos_qualified.csv (companies meeting thresholds)

ESOS thresholds:
- Route 1: 250+ UK employees
- Route 2: £44m+ turnover AND £38m+ balance sheet (BOTH required)

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage2_filter_qualified \
        --input data/processed/xbrl_extracted.csv \
        --output data/processed/esos_qualified.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from radar_intel_core.esos.thresholds import (
    meets_esos_threshold,
    has_sufficient_data,
    EMPLOYEE_THRESHOLD,
    TURNOVER_THRESHOLD,
    BALANCE_SHEET_THRESHOLD,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def filter_qualified(
    input_path: Path,
    output_path: Path,
) -> int:
    """
    Filter XBRL extracted data to companies meeting ESOS thresholds.

    Args:
        input_path: Path to xbrl_extracted.csv
        output_path: Path for output CSV

    Returns:
        Number of qualified companies
    """
    logger.info(f"Loading {input_path}")
    df = pd.read_csv(input_path)

    initial_count = len(df)
    logger.info(f"Loaded {initial_count:,} companies")

    # Check data availability stats
    has_employees = int(df["employees"].notna().sum())
    has_turnover = int(df["turnover"].notna().sum())
    has_balance = int(df["balance_sheet"].notna().sum())
    has_both_financial = int((df["turnover"].notna() & df["balance_sheet"].notna()).sum())

    logger.info("Data availability:")
    logger.info(f"  - Employees disclosed: {has_employees:,} ({100*has_employees/initial_count:.1f}%)")
    logger.info(f"  - Turnover disclosed: {has_turnover:,} ({100*has_turnover/initial_count:.1f}%)")
    logger.info(f"  - Balance sheet disclosed: {has_balance:,} ({100*has_balance/initial_count:.1f}%)")
    logger.info(f"  - Both turnover AND balance sheet: {has_both_financial:,} ({100*has_both_financial/initial_count:.1f}%)")

    # Apply threshold checks
    results: list[dict[str, Any]] = []

    for _, row in df.iterrows():
        employees = row.get("employees")
        turnover = row.get("turnover")
        balance_sheet = row.get("balance_sheet")

        # Convert NaN to None
        if pd.isna(employees):
            employees = None
        else:
            employees = int(employees)

        if pd.isna(turnover):
            turnover = None

        if pd.isna(balance_sheet):
            balance_sheet = None

        # Check if we have enough data
        if not has_sufficient_data(employees, turnover, balance_sheet):
            continue

        # Check thresholds
        result = meets_esos_threshold(employees, turnover, balance_sheet)

        if result.qualifies:
            results.append({
                "company_number": row["company_number"],
                "company_name": row.get("company_name"),
                "employees": employees,
                "turnover": turnover,
                "balance_sheet": balance_sheet,
                "accounts_date": row.get("accounts_date"),
                "qualification_route": result.route,
            })

    qualified_df = pd.DataFrame(results)

    # Log qualification breakdown
    if len(qualified_df) > 0:
        # Count by qualification route
        route_counts = qualified_df["qualification_route"].value_counts()
        employee_route = route_counts.get("EMPLOYEE_TEST", 0)
        financial_route = route_counts.get("FINANCIAL_TEST", 0)

        logger.info("Qualification breakdown:")
        logger.info(f"  - Employee test (≥{EMPLOYEE_THRESHOLD}): {employee_route:,}")
        logger.info(f"  - Financial test (≥£{TURNOVER_THRESHOLD/1e6:.0f}m + ≥£{BALANCE_SHEET_THRESHOLD/1e6:.0f}m): {financial_route:,}")
        logger.info(f"  - Total qualified: {len(qualified_df):,}")
    else:
        logger.warning("No companies met ESOS thresholds!")

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    qualified_df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(qualified_df):,} qualified companies to {output_path}")

    return len(qualified_df)


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Filter to companies meeting ESOS thresholds"
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to xbrl_extracted.csv",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path for output CSV",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        sys.exit(1)

    count = filter_qualified(input_path, output_path)

    if count == 0:
        logger.warning("No qualified companies found - check input data")
        sys.exit(1)


if __name__ == "__main__":
    main()
