"""
ESOS-Radar Pipeline Orchestrator.

Runs all 5 stages in sequence:
1. Extract XBRL data
2. Filter to ESOS-qualified companies
3. Find gaps (not in notifications)
4. Check parent coverage
5. Apply hygiene filters

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.run_pipeline \
        --xbrl-zip data/raw/xbrl/Accounts_Monthly_Data_December2024.zip \
        --notifications data/raw/esos_phase3_notifications.xlsx \
        --output-dir data/processed

Or run individual stages:
    python -m apps.esos_radar.esos_radar.pipeline.run_pipeline \
        --stage 1 \
        --xbrl-zip data/raw/xbrl/Accounts_Monthly_Data_December2024.zip \
        --output-dir data/processed
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from datetime import datetime

from .stage1_extract_xbrl import extract_from_zip
from .stage2_filter_qualified import filter_qualified
from .stage3_find_gaps import find_gaps
from .stage4_check_parents import check_all_parents
from .stage5_apply_hygiene import apply_hygiene_filters


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def run_pipeline(
    xbrl_zip: Path,
    notifications: Path,
    output_dir: Path,
    start_stage: int = 1,
    end_stage: int = 5,
    xbrl_limit: int | None = None,
    skip_parent_check: bool = False,
) -> dict:
    """
    Run the ESOS-Radar pipeline.
    
    Args:
        xbrl_zip: Path to Companies House XBRL ZIP file
        notifications: Path to ESOS Phase 3 notifications workbook
        output_dir: Directory for output files
        start_stage: Stage to start from (1-5)
        end_stage: Stage to end at (1-5)
        xbrl_limit: Limit files to process in Stage 1 (for testing)
        skip_parent_check: Skip API calls in Stage 4 (for testing)
    
    Returns:
        Dict with counts from each stage
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Define intermediate file paths
    xbrl_extracted = output_dir / "xbrl_extracted.csv"
    esos_qualified = output_dir / "esos_qualified.csv"
    gap_candidates = output_dir / "gap_candidates.csv"
    verified_gaps = output_dir / "verified_gaps.csv"
    tier_a_plus = output_dir / "tier_a_plus_leads.csv"
    
    results = {}
    
    start_time = datetime.now()
    logger.info(f"Starting ESOS-Radar pipeline at {start_time}")
    logger.info(f"Running stages {start_stage} to {end_stage}")
    
    # Stage 1: Extract XBRL
    if start_stage <= 1 <= end_stage:
        logger.info("=" * 60)
        logger.info("STAGE 1: Extract XBRL data")
        logger.info("=" * 60)
        
        if not xbrl_zip.exists():
            logger.error(f"XBRL ZIP not found: {xbrl_zip}")
            sys.exit(1)
        
        results["stage1_extracted"] = extract_from_zip(
            xbrl_zip, xbrl_extracted, limit=xbrl_limit
        )
    
    # Stage 2: Filter qualified
    if start_stage <= 2 <= end_stage:
        logger.info("=" * 60)
        logger.info("STAGE 2: Filter to ESOS-qualified companies")
        logger.info("=" * 60)
        
        if not xbrl_extracted.exists():
            logger.error(f"Stage 1 output not found: {xbrl_extracted}")
            logger.error("Run Stage 1 first or provide existing xbrl_extracted.csv")
            sys.exit(1)
        
        results["stage2_qualified"] = filter_qualified(xbrl_extracted, esos_qualified)
    
    # Stage 3: Find gaps
    if start_stage <= 3 <= end_stage:
        logger.info("=" * 60)
        logger.info("STAGE 3: Find gap candidates")
        logger.info("=" * 60)
        
        if not esos_qualified.exists():
            logger.error(f"Stage 2 output not found: {esos_qualified}")
            sys.exit(1)
        
        if not notifications.exists():
            logger.error(f"Notifications workbook not found: {notifications}")
            sys.exit(1)
        
        results["stage3_gaps"] = find_gaps(esos_qualified, notifications, gap_candidates)
    
    # Stage 4: Check parents
    if start_stage <= 4 <= end_stage:
        logger.info("=" * 60)
        logger.info("STAGE 4: Check parent coverage")
        logger.info("=" * 60)
        
        if not gap_candidates.exists():
            logger.error(f"Stage 3 output not found: {gap_candidates}")
            sys.exit(1)
        
        results["stage4_verified"] = check_all_parents(
            gap_candidates, notifications, verified_gaps,
            skip_parent_check=skip_parent_check
        )
    
    # Stage 5: Apply hygiene
    if start_stage <= 5 <= end_stage:
        logger.info("=" * 60)
        logger.info("STAGE 5: Apply hygiene filters")
        logger.info("=" * 60)
        
        if not verified_gaps.exists():
            logger.error(f"Stage 4 output not found: {verified_gaps}")
            sys.exit(1)
        
        results["stage5_final"] = apply_hygiene_filters(
            verified_gaps, tier_a_plus,
            skip_enrichment=skip_parent_check  # If skipping parent check, also skip enrichment
        )
    
    # Summary
    end_time = datetime.now()
    duration = end_time - start_time
    
    logger.info("=" * 60)
    logger.info("PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Duration: {duration}")
    logger.info(f"Results:")
    for key, value in results.items():
        logger.info(f"  - {key}: {value:,}")
    
    if "stage5_final" in results:
        logger.info(f"\nFinal output: {tier_a_plus}")
        logger.info(f"Tier A+ leads: {results['stage5_final']:,}")
    
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run ESOS-Radar pipeline"
    )
    parser.add_argument(
        "--xbrl-zip",
        required=True,
        help="Path to Companies House XBRL ZIP file",
    )
    parser.add_argument(
        "--notifications",
        required=True,
        help="Path to ESOS Phase 3 notifications workbook",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory for output files",
    )
    parser.add_argument(
        "--start-stage",
        type=int,
        default=1,
        choices=[1, 2, 3, 4, 5],
        help="Stage to start from (default: 1)",
    )
    parser.add_argument(
        "--end-stage",
        type=int,
        default=5,
        choices=[1, 2, 3, 4, 5],
        help="Stage to end at (default: 5)",
    )
    parser.add_argument(
        "--xbrl-limit",
        type=int,
        default=None,
        help="Limit number of XBRL files to process (for testing)",
    )
    parser.add_argument(
        "--skip-parent-check",
        action="store_true",
        help="Skip parent coverage API calls (for testing)",
    )
    
    args = parser.parse_args()
    
    run_pipeline(
        xbrl_zip=Path(args.xbrl_zip),
        notifications=Path(args.notifications),
        output_dir=Path(args.output_dir),
        start_stage=args.start_stage,
        end_stage=args.end_stage,
        xbrl_limit=args.xbrl_limit,
        skip_parent_check=args.skip_parent_check,
    )


if __name__ == "__main__":
    main()
