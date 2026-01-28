"""
ESOS Lead Merge Utility

Combines leads from multiple XBRL processing runs into a single deduplicated file.
Handles the common case where the same company appears in multiple months' data.

Usage:
    python merge_leads.py \
        --inputs data/dec2025_leads.csv data/nov2025_leads.csv data/oct2025_leads.csv \
        --output data/merged_leads.csv

    # Or with glob pattern:
    python merge_leads.py \
        --pattern "data/processed/*_tier_a_plus.csv" \
        --output data/merged_leads.csv

Deduplication logic:
    - Companies matched by company_number (primary key)
    - When duplicates found, keeps the record with:
      1. Most recent accounts_date
      2. Highest employee count (tie-breaker)
      3. First file processed (final tie-breaker)

Drop this file into: apps/esos_radar/esos_radar/pipeline/merge_leads.py
"""

import argparse
import glob
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def parse_date_flexible(date_str: str) -> Optional[datetime]:
    """
    Parse date string with multiple format support.

    Args:
        date_str: Date string in various formats

    Returns:
        datetime object or None if parsing fails
    """
    if pd.isna(date_str) or not date_str:
        return None

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(str(date_str).strip(), fmt)
        except ValueError:
            continue

    return None


def merge_lead_files(
    input_files: List[str],
    output_path: str,
    prefer_recent: bool = True,
    keep_source_column: bool = True,
) -> pd.DataFrame:
    """
    Merge multiple lead CSV files with intelligent deduplication.

    Args:
        input_files: List of paths to CSV files to merge
        output_path: Path for output merged CSV
        prefer_recent: If True, prefer records with more recent accounts_date
        keep_source_column: If True, add column showing which file each lead came from

    Returns:
        Merged DataFrame
    """
    if not input_files:
        raise ValueError("No input files provided")

    # Load all files
    dfs = []
    for file_path in input_files:
        path = Path(file_path)
        if not path.exists():
            logger.warning(f"File not found, skipping: {file_path}")
            continue

        logger.info(f"Loading: {file_path}")
        df = pd.read_csv(file_path, dtype={"company_number": str})

        # Add source file column
        if keep_source_column:
            df["_source_file"] = path.name

        # Parse accounts_date for comparison
        if "accounts_date" in df.columns:
            df["_accounts_date_parsed"] = df["accounts_date"].apply(parse_date_flexible)
        else:
            df["_accounts_date_parsed"] = None

        dfs.append(df)
        logger.info(f"  → {len(df):,} leads")

    if not dfs:
        raise ValueError("No valid input files found")

    # Concatenate all DataFrames
    combined = pd.concat(dfs, ignore_index=True)
    total_before = len(combined)
    logger.info(f"\nTotal leads before dedup: {total_before:,}")

    # Normalise company numbers for matching
    combined["_company_number_norm"] = (
        combined["company_number"]
        .str.upper()
        .str.strip()
        .str.zfill(8)
    )

    # Count duplicates
    dup_counts = combined["_company_number_norm"].value_counts()
    duplicates = dup_counts[dup_counts > 1]
    logger.info(f"Companies appearing multiple times: {len(duplicates):,}")

    # Sort for deduplication priority
    # 1. Most recent accounts_date first
    # 2. Highest employee count
    # 3. First file (original order preserved by stable sort)

    if prefer_recent and "_accounts_date_parsed" in combined.columns:
        combined = combined.sort_values(
            by=["_accounts_date_parsed", "employees"],
            ascending=[False, False],
            na_position="last",
        )
    else:
        combined = combined.sort_values(
            by=["employees"],
            ascending=[False],
            na_position="last",
        )

    # Deduplicate - keep first (best) record for each company
    deduplicated = combined.drop_duplicates(
        subset=["_company_number_norm"],
        keep="first",
    )

    total_after = len(deduplicated)
    removed = total_before - total_after
    logger.info(f"Leads after dedup: {total_after:,} (removed {removed:,} duplicates)")

    # Clean up internal columns
    drop_cols = ["_company_number_norm", "_accounts_date_parsed"]
    if not keep_source_column:
        drop_cols.append("_source_file")
    deduplicated = deduplicated.drop(columns=[c for c in drop_cols if c in deduplicated.columns])

    # Sort output by employees descending (best leads first)
    if "employees" in deduplicated.columns:
        deduplicated = deduplicated.sort_values("employees", ascending=False)

    # Reset index
    deduplicated = deduplicated.reset_index(drop=True)

    # Save
    output_dir = Path(output_path).parent
    output_dir.mkdir(parents=True, exist_ok=True)
    deduplicated.to_csv(output_path, index=False)
    logger.info(f"\nSaved merged leads to: {output_path}")

    return deduplicated


def _load_and_normalise(file_path: str) -> pd.DataFrame:
    """
    Load a leads CSV and add normalised company number column.

    Args:
        file_path: Path to CSV file

    Returns:
        DataFrame with _cn_norm column added
    """
    df = pd.read_csv(file_path, dtype={"company_number": str})
    df["_cn_norm"] = df["company_number"].str.upper().str.strip().str.zfill(8)
    return df


def merge_with_existing(
    new_file: str,
    existing_file: str,
    output_path: str,
) -> pd.DataFrame:
    """
    Merge a new batch of leads with an existing master file.

    Identifies:
    - Genuinely new leads (not in existing)
    - Updated leads (in both, newer data available)
    - Unchanged leads (in both, no updates)

    Args:
        new_file: Path to new leads CSV
        existing_file: Path to existing master CSV
        output_path: Path for merged output

    Returns:
        Merged DataFrame
    """
    logger.info(f"Merging new leads from: {new_file}")
    logger.info(f"With existing master: {existing_file}")

    new_df = _load_and_normalise(new_file)
    existing_df = _load_and_normalise(existing_file)

    existing_numbers = set(existing_df["_cn_norm"])
    new_numbers = set(new_df["_cn_norm"])

    # Identify categories
    genuinely_new = new_numbers - existing_numbers
    overlapping = new_numbers & existing_numbers

    logger.info(f"\nNew leads: {len(new_df):,}")
    logger.info(f"Existing leads: {len(existing_df):,}")
    logger.info(f"Genuinely new: {len(genuinely_new):,}")
    logger.info(f"Overlapping: {len(overlapping):,}")

    # Add status column to new leads
    new_df["_merge_status"] = new_df["_cn_norm"].apply(
        lambda x: "NEW" if x in genuinely_new else "UPDATE"
    )

    # Merge using the standard function
    merged = merge_lead_files(
        [new_file, existing_file],
        output_path,
        prefer_recent=True,
        keep_source_column=True,
    )

    # Report
    if "_source_file" in merged.columns:
        source_counts = merged["_source_file"].value_counts()
        logger.info("\nLeads by source:")
        for source, count in source_counts.items():
            logger.info(f"  {source}: {count:,}")

    return merged


def generate_diff_report(
    new_file: str,
    existing_file: str,
    output_path: str,
) -> pd.DataFrame:
    """
    Generate a report of differences between two lead files.

    Useful for understanding what a new XBRL month adds to the lead pool.

    Args:
        new_file: Path to new leads CSV
        existing_file: Path to existing leads CSV
        output_path: Path for diff report CSV

    Returns:
        DataFrame with only genuinely new leads
    """
    new_df = _load_and_normalise(new_file)
    existing_df = _load_and_normalise(existing_file)

    existing_numbers = set(existing_df["_cn_norm"])

    # Filter to genuinely new
    genuinely_new = new_df[~new_df["_cn_norm"].isin(existing_numbers)].copy()
    genuinely_new = genuinely_new.drop(columns=["_cn_norm"])

    logger.info(f"\nDiff report:")
    logger.info(f"  New file leads: {len(new_df):,}")
    logger.info(f"  Existing leads: {len(existing_df):,}")
    logger.info(f"  Genuinely new: {len(genuinely_new):,}")

    # Save
    genuinely_new.to_csv(output_path, index=False)
    logger.info(f"  Saved to: {output_path}")

    return genuinely_new


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple ESOS lead files with deduplication"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Merge command
    merge_parser = subparsers.add_parser("merge", help="Merge multiple files")
    merge_parser.add_argument(
        "--inputs",
        nargs="+",
        help="Input CSV files to merge",
    )
    merge_parser.add_argument(
        "--pattern",
        help="Glob pattern for input files (e.g., 'data/*_leads.csv')",
    )
    merge_parser.add_argument(
        "--output",
        required=True,
        help="Output path for merged CSV",
    )
    merge_parser.add_argument(
        "--no-source-column",
        action="store_true",
        help="Don't add column showing source file",
    )

    # Update command
    update_parser = subparsers.add_parser(
        "update",
        help="Add new leads to existing master file"
    )
    update_parser.add_argument(
        "--new",
        required=True,
        help="New leads file",
    )
    update_parser.add_argument(
        "--existing",
        required=True,
        help="Existing master file",
    )
    update_parser.add_argument(
        "--output",
        required=True,
        help="Output path for updated master",
    )

    # Diff command
    diff_parser = subparsers.add_parser(
        "diff",
        help="Show only leads in new file that aren't in existing"
    )
    diff_parser.add_argument(
        "--new",
        required=True,
        help="New leads file",
    )
    diff_parser.add_argument(
        "--existing",
        required=True,
        help="Existing file to compare against",
    )
    diff_parser.add_argument(
        "--output",
        required=True,
        help="Output path for diff report",
    )

    args = parser.parse_args()

    if args.command == "merge":
        # Get input files
        if args.pattern:
            input_files = sorted(glob.glob(args.pattern))
            if not input_files:
                logger.error(f"No files match pattern: {args.pattern}")
                return
        elif args.inputs:
            input_files = args.inputs
        else:
            logger.error("Must provide --inputs or --pattern")
            return

        merge_lead_files(
            input_files=input_files,
            output_path=args.output,
            keep_source_column=not args.no_source_column,
        )

    elif args.command == "update":
        merge_with_existing(
            new_file=args.new,
            existing_file=args.existing,
            output_path=args.output,
        )

    elif args.command == "diff":
        generate_diff_report(
            new_file=args.new,
            existing_file=args.existing,
            output_path=args.output,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()