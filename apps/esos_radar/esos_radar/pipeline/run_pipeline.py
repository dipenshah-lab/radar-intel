#!/usr/bin/env python3
"""
ESOS-Radar Pipeline Orchestrator

Runs the complete 7-stage pipeline with proper error handling,
progress tracking, and optional resume capability.

Usage:
    # Full run from XBRL file to verified leads
    python run_pipeline.py \
        --xbrl data/raw/xbrl/Accounts_Monthly_Data-December2024.zip \
        --notifications data/raw/esos_phase3_notifications.xlsx \
        --output-dir data/processed/dec2024

    # Resume from a specific stage
    python run_pipeline.py \
        --resume-from stage4 \
        --work-dir data/processed/dec2024 \
        --notifications data/raw/esos_phase3_notifications.xlsx

    # Skip enrichment stages (stages 6, 6b) for quick testing
    python run_pipeline.py \
        --xbrl data/raw/xbrl/Accounts_Monthly_Data-December2024.zip \
        --notifications data/raw/esos_phase3_notifications.xlsx \
        --output-dir data/processed/dec2024 \
        --stop-after stage5

Drop this file into: apps/esos_radar/esos_radar/pipeline/run_pipeline.py
"""

import argparse
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class StageConfig:
    """Configuration for a pipeline stage."""
    name: str
    module: str
    input_arg: str
    output_arg: str
    extra_args: Dict[str, str] = None
    description: str = ""


@dataclass
class StageResult:
    """Result of running a pipeline stage."""
    stage: str
    success: bool
    start_time: str
    end_time: str
    duration_seconds: float
    output_file: str
    output_rows: int = 0
    error_message: str = ""


# Define the pipeline stages
PIPELINE_STAGES = [
    StageConfig(
        name="stage1",
        module="apps.esos_radar.esos_radar.pipeline.stage1_extract_xbrl",
        input_arg="--input",
        output_arg="--output",
        description="Extract financial data from XBRL files",
    ),
    StageConfig(
        name="stage2",
        module="apps.esos_radar.esos_radar.pipeline.stage2_filter_qualified",
        input_arg="--input",
        output_arg="--output",
        description="Filter companies meeting ESOS thresholds",
    ),
    StageConfig(
        name="stage3",
        module="apps.esos_radar.esos_radar.pipeline.stage3_find_gaps",
        input_arg="--input",
        output_arg="--output",
        extra_args={"--notifications": "NOTIFICATIONS_FILE"},
        description="Exclude companies already on notification list",
    ),
    StageConfig(
        name="stage4",
        module="apps.esos_radar.esos_radar.pipeline.stage4_check_parents",
        input_arg="--input",
        output_arg="--output",
        extra_args={"--notifications": "NOTIFICATIONS_FILE"},
        description="Check parent company coverage via PSC data",
    ),
    StageConfig(
        name="stage5",
        module="apps.esos_radar.esos_radar.pipeline.stage5_apply_hygiene",
        input_arg="--input",
        output_arg="--output",
        description="Apply hygiene filters and deduplicate groups",
    ),
    StageConfig(
        name="stage6",
        module="apps.esos_radar.esos_radar.pipeline.stage6_enrich_contacts",
        input_arg="--input",
        output_arg="--output",
        description="Find domains and director contacts",
    ),
    StageConfig(
        name="stage6b",
        module="apps.esos_radar.esos_radar.pipeline.stage6b_verify_leads",
        input_arg="--input",
        output_arg="--output",
        description="Verify emails via NeverBounce",
    ),
]

# Output file names for each stage
STAGE_OUTPUTS = {
    "stage1": "xbrl_extracted.csv",
    "stage2": "esos_qualified.csv",
    "stage3": "gap_candidates.csv",
    "stage4": "verified_gaps.csv",
    "stage5": "tier_a_plus_leads.csv",
    "stage6": "enriched_leads.csv",
    "stage6b": "verified_leads.csv",
}


class PipelineRunner:
    """
    Orchestrates the ESOS-Radar pipeline stages.
    """

    def __init__(
            self,
            work_dir: Path,
            notifications_file: Path,
            xbrl_file: Optional[Path] = None,
            use_cache: bool = True,
            cache_path: Optional[Path] = None,
    ):
        """
        Initialize the pipeline runner.

        Args:
            work_dir: Directory for intermediate and output files
            notifications_file: Path to ESOS Phase 3 notifications workbook
            xbrl_file: Path to XBRL ZIP file (required for stage1)
            use_cache: Whether to use API caching
            cache_path: Path to cache database
        """
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        self.notifications_file = Path(notifications_file)
        self.xbrl_file = Path(xbrl_file) if xbrl_file else None
        self.use_cache = use_cache
        self.cache_path = cache_path or self.work_dir / "ch_cache.db"

        self.results: List[StageResult] = []
        self.state_file = self.work_dir / "pipeline_state.json"

    def _get_stage_input(self, stage_name: str) -> Path:
        """Get the input file path for a stage."""
        stage_idx = [s.name for s in PIPELINE_STAGES].index(stage_name)

        if stage_idx == 0:
            # Stage 1 uses XBRL file
            if not self.xbrl_file:
                raise ValueError("XBRL file required for stage1")
            return self.xbrl_file
        else:
            # Use output of previous stage
            prev_stage = PIPELINE_STAGES[stage_idx - 1].name
            return self.work_dir / STAGE_OUTPUTS[prev_stage]

    def _get_stage_output(self, stage_name: str) -> Path:
        """Get the output file path for a stage."""
        return self.work_dir / STAGE_OUTPUTS[stage_name]

    def _build_command(self, stage: StageConfig) -> List[str]:
        """Build the command line for a stage."""
        cmd = [
            sys.executable, "-m", stage.module,
            stage.input_arg, str(self._get_stage_input(stage.name)),
            stage.output_arg, str(self._get_stage_output(stage.name)),
        ]

        # Add extra arguments
        if stage.extra_args:
            for arg, value in stage.extra_args.items():
                if value == "NOTIFICATIONS_FILE":
                    cmd.extend([arg, str(self.notifications_file)])
                else:
                    cmd.extend([arg, value])

        return cmd

    @staticmethod
    def _count_rows(file_path: Path) -> int:
        """Count rows in a CSV file (excluding header)."""
        if not file_path.exists():
            return 0
        with open(file_path) as f:
            return sum(1 for _ in f) - 1  # Subtract header

    def run_stage(self, stage: StageConfig) -> StageResult:
        """
        Run a single pipeline stage.

        Args:
            stage: Stage configuration

        Returns:
            StageResult with timing and status info
        """
        logger.info(f"\n{'=' * 60}")
        logger.info(f"Stage: {stage.name} - {stage.description}")
        logger.info(f"{'=' * 60}")

        start_time = datetime.now()
        output_file = self._get_stage_output(stage.name)

        try:
            cmd = self._build_command(stage)
            logger.info(f"Command: {' '.join(cmd)}")

            # Run the stage
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour timeout
            )

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            if result.returncode != 0:
                logger.error(f"Stage {stage.name} failed!")
                logger.error(f"STDERR: {result.stderr}")
                return StageResult(
                    stage=stage.name,
                    success=False,
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                    duration_seconds=duration,
                    output_file=str(output_file),
                    error_message=result.stderr,
                )

            # Log stdout
            if result.stdout:
                for line in result.stdout.strip().split("\n"):
                    logger.info(f"  {line}")

            # Count output rows
            output_rows = self._count_rows(output_file)

            logger.info(f"✓ Stage {stage.name} completed in {duration:.1f}s")
            logger.info(f"  Output: {output_file} ({output_rows:,} rows)")

            return StageResult(
                stage=stage.name,
                success=True,
                start_time=start_time.isoformat(),
                end_time=end_time.isoformat(),
                duration_seconds=duration,
                output_file=str(output_file),
                output_rows=output_rows,
            )

        except subprocess.TimeoutExpired:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.error(f"Stage {stage.name} timed out after {duration:.1f}s")
            return StageResult(
                stage=stage.name,
                success=False,
                start_time=start_time.isoformat(),
                end_time=end_time.isoformat(),
                duration_seconds=duration,
                output_file=str(output_file),
                error_message="Stage timed out",
            )

        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.error(f"Stage {stage.name} error: {e}")
            return StageResult(
                stage=stage.name,
                success=False,
                start_time=start_time.isoformat(),
                end_time=end_time.isoformat(),
                duration_seconds=duration,
                output_file=str(output_file),
                error_message=str(e),
            )

    def save_state(self):
        """Save pipeline state to JSON file."""
        state = {
            "work_dir": str(self.work_dir),
            "xbrl_file": str(self.xbrl_file) if self.xbrl_file else None,
            "notifications_file": str(self.notifications_file),
            "results": [asdict(r) for r in self.results],
            "updated_at": datetime.now().isoformat(),
        }
        with open(self.state_file, "w") as f:
            json.dump(state, f, indent=2)
        logger.info(f"State saved to: {self.state_file}")

    def load_state(self) -> bool:
        """
        Load pipeline state from JSON file.

        Returns:
            True if state was loaded, False if no state file exists
        """
        if not self.state_file.exists():
            return False

        with open(self.state_file) as f:
            state = json.load(f)

        self.results = [StageResult(**r) for r in state.get("results", [])]
        logger.info(f"Loaded state: {len(self.results)} stages completed previously")
        return True

    def get_last_successful_stage(self) -> Optional[str]:
        """Get the name of the last successfully completed stage."""
        for result in reversed(self.results):
            if result.success:
                return result.stage
        return None

    def run(
            self,
            start_stage: str = "stage1",
            stop_stage: str = "stage6b",
            resume: bool = False,
    ) -> bool:
        """
        Run the pipeline from start_stage to stop_stage.

        Args:
            start_stage: Stage to start from
            stop_stage: Stage to stop after
            resume: If True, resume from last successful stage

        Returns:
            True if all stages completed successfully
        """
        pipeline_start = datetime.now()

        # Get stage indices
        stage_names = [s.name for s in PIPELINE_STAGES]

        if start_stage not in stage_names:
            logger.error(f"Unknown start stage: {start_stage}")
            return False
        if stop_stage not in stage_names:
            logger.error(f"Unknown stop stage: {stop_stage}")
            return False

        start_idx = stage_names.index(start_stage)
        stop_idx = stage_names.index(stop_stage)

        if start_idx > stop_idx:
            logger.error("Start stage must come before stop stage")
            return False

        # Handle resume
        if resume and self.load_state():
            last_success = self.get_last_successful_stage()
            if last_success:
                resume_idx = stage_names.index(last_success) + 1
                if resume_idx > start_idx:
                    start_idx = resume_idx
                    logger.info(f"Resuming from: {stage_names[start_idx]}")

        # Run stages
        stages_to_run = PIPELINE_STAGES[start_idx:stop_idx + 1]

        logger.info(f"\n{'#' * 60}")
        logger.info(f"ESOS-Radar Pipeline")
        logger.info(f"Running stages: {stages_to_run[0].name} → {stages_to_run[-1].name}")
        logger.info(f"Work directory: {self.work_dir}")
        logger.info(f"{'#' * 60}\n")

        all_success = True

        for stage in stages_to_run:
            result = self.run_stage(stage)
            self.results.append(result)
            self.save_state()

            if not result.success:
                all_success = False
                logger.error(f"\n❌ Pipeline stopped at {stage.name}")
                break

        # Final summary
        pipeline_end = datetime.now()
        total_duration = (pipeline_end - pipeline_start).total_seconds()

        logger.info(f"\n{'=' * 60}")
        logger.info("PIPELINE SUMMARY")
        logger.info(f"{'=' * 60}")
        logger.info(f"Total duration: {total_duration:.1f}s ({total_duration / 60:.1f} minutes)")
        logger.info(f"Status: {'✓ SUCCESS' if all_success else '❌ FAILED'}")
        logger.info("")

        for result in self.results[-len(stages_to_run):]:
            status = "✓" if result.success else "❌"
            logger.info(
                f"  {status} {result.stage}: {result.output_rows:,} rows "
                f"({result.duration_seconds:.1f}s)"
            )

        if all_success:
            final_output = self._get_stage_output(stages_to_run[-1].name)
            logger.info(f"\n📁 Final output: {final_output}")

        return all_success


def main():
    parser = argparse.ArgumentParser(
        description="Run the ESOS-Radar pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full pipeline run
  python run_pipeline.py \\
      --xbrl data/raw/xbrl/Accounts_Monthly_Data-December2024.zip \\
      --notifications data/raw/esos_phase3_notifications.xlsx \\
      --output-dir data/processed/dec2024

  # Resume from failure
  python run_pipeline.py \\
      --work-dir data/processed/dec2024 \\
      --notifications data/raw/esos_phase3_notifications.xlsx \\
      --resume

  # Run only stages 1-5 (skip contact enrichment)
  python run_pipeline.py \\
      --xbrl data/raw/xbrl/Accounts_Monthly_Data-December2024.zip \\
      --notifications data/raw/esos_phase3_notifications.xlsx \\
      --output-dir data/processed/dec2024 \\
      --stop-after stage5
        """,
    )

    parser.add_argument(
        "--xbrl",
        help="Path to XBRL ZIP file (required for stage1)",
    )
    parser.add_argument(
        "--notifications",
        required=True,
        help="Path to ESOS Phase 3 notifications workbook",
    )
    parser.add_argument(
        "--output-dir",
        "--work-dir",
        dest="work_dir",
        required=True,
        help="Directory for output files",
    )
    parser.add_argument(
        "--start-from",
        default="stage1",
        choices=["stage1", "stage2", "stage3", "stage4", "stage5", "stage6", "stage6b"],
        help="Stage to start from (default: stage1)",
    )
    parser.add_argument(
        "--stop-after",
        default="stage6b",
        choices=["stage1", "stage2", "stage3", "stage4", "stage5", "stage6", "stage6b"],
        help="Stage to stop after (default: stage6b)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last successful stage",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable Companies House API caching",
    )

    args = parser.parse_args()

    # Validate XBRL file requirement
    if args.start_from == "stage1" and not args.xbrl:
        parser.error("--xbrl is required when starting from stage1")

    runner = PipelineRunner(
        work_dir=Path(args.work_dir),
        notifications_file=Path(args.notifications),
        xbrl_file=Path(args.xbrl) if args.xbrl else None,
        use_cache=not args.no_cache,
    )

    success = runner.run(
        start_stage=args.start_from,
        stop_stage=args.stop_after,
        resume=args.resume,
    )

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()