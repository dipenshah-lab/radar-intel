"""
Stage 6b: Prepare email variants for NeverBounce verification and merge results.

Two modes:
1. prepare: Extract email variants into NeverBounce format
2. merge: Merge verification results back into leads

Usage:
    # Prepare for NeverBounce
    python -m apps.esos_radar.esos_radar.pipeline.stage6b_verify_emails prepare \
        --input data/processed/enriched_leads.csv \
        --output data/processed/neverbounce_input.csv

    # After NeverBounce verification, merge results
    python -m apps.esos_radar.esos_radar.pipeline.stage6b_verify_emails merge \
        --original data/processed/enriched_leads.csv \
        --results neverbounce_results.csv \
        --output data/processed/final_leads.csv

NeverBounce pricing (as of 2024):
    - Pay as you go: ~$0.008/email
    - 86 leads × 4 variants = 344 emails ≈ $2.75 (~£2.20)
"""

from __future__ import annotations

import argparse
import csv
import logging
from pathlib import Path
from typing import Dict, List

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def prepare_for_neverbounce(
        input_path: Path,
        output_path: Path,
) -> Dict[str, int]:
    """
    Extract email variants into NeverBounce format.

    Args:
        input_path: Path to enriched_leads.csv
        output_path: Path for NeverBounce input CSV

    Returns:
        Stats dict with counts
    """
    stats = {"companies": 0, "variants": 0, "skipped": 0}
    variants_list = []

    df = pd.read_csv(input_path, dtype={"company_number": str})

    for _, row in df.iterrows():
        company_number = row.get("company_number", "")
        email_variants = row.get("email_variants", "")

        if not email_variants or pd.isna(email_variants):
            stats["skipped"] += 1
            continue

        stats["companies"] += 1

        # Parse variants (semicolon separated)
        variants = [v.strip() for v in str(email_variants).split(";") if v.strip()]

        for idx, email in enumerate(variants):
            custom_id = f"{company_number}_{idx}"
            variants_list.append({
                "email": email,
                "custom_id": custom_id
            })
            stats["variants"] += 1

    # Write NeverBounce format (with custom_id for mapping back)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=["email", "custom_id"])
        writer.writeheader()
        writer.writerows(variants_list)

    # Also write simple email-only version
    simple_output = output_path.with_suffix('.simple.csv')
    with open(simple_output, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["email"])
        for v in variants_list:
            writer.writerow([v["email"]])

    logger.info(f"Prepared {stats['variants']:,} email variants from {stats['companies']:,} companies")
    logger.info(f"Skipped {stats['skipped']:,} companies without email variants")
    logger.info(f"Output: {output_path}")
    logger.info(f"Also created: {simple_output}")

    return stats


def merge_neverbounce_results(
        original_path: Path,
        results_path: Path,
        output_path: Path,
) -> Dict[str, int]:
    """
    Merge NeverBounce results back to original leads.

    Selects the best valid email for each company.

    Args:
        original_path: Path to enriched_leads.csv
        results_path: Path to NeverBounce results CSV
        output_path: Path for final output CSV

    Returns:
        Stats dict with counts
    """
    stats = {"companies": 0, "with_valid_email": 0, "no_valid_email": 0}

    # Load NeverBounce results
    # Build mapping: company_number -> list of (email, result, variant_idx)
    company_emails: Dict[str, List[tuple]] = {}

    results_df = pd.read_csv(results_path)

    for _, row in results_df.iterrows():
        email = row.get("email", "")
        result = str(row.get("result", "")).lower()
        custom_id = str(row.get("custom_id", ""))

        # Parse custom_id back to company_number
        if "_" in custom_id:
            company_number, variant_idx = custom_id.rsplit("_", 1)
            try:
                variant_idx = int(variant_idx)
            except ValueError:
                continue
        else:
            continue

        if company_number not in company_emails:
            company_emails[company_number] = []

        company_emails[company_number].append((email, result, variant_idx))

    # Load original leads
    df = pd.read_csv(original_path, dtype={"company_number": str})

    # Result priority: valid > catchall > unknown > invalid > disposable
    result_priority = {"valid": 0, "catchall": 1, "unknown": 2, "invalid": 3, "disposable": 4}

    verified_emails = []
    email_statuses = []
    all_results = []

    for _, row in df.iterrows():
        company_number = str(row.get("company_number", ""))
        stats["companies"] += 1

        if company_number in company_emails:
            emails = company_emails[company_number]

            # Sort by result priority, then variant index
            emails.sort(key=lambda x: (result_priority.get(x[1], 99), x[2]))

            best_email, best_result, _ = emails[0]

            if best_result in ("valid", "catchall"):
                verified_emails.append(best_email)
                email_statuses.append(best_result)
                stats["with_valid_email"] += 1
            else:
                verified_emails.append("")
                email_statuses.append(best_result)
                stats["no_valid_email"] += 1

            # Store all results for reference
            all_results.append("; ".join([f"{e}:{r}" for e, r, _ in emails]))
        else:
            verified_emails.append("")
            email_statuses.append("not_checked")
            all_results.append("")
            stats["no_valid_email"] += 1

    # Add columns
    df["verified_email"] = verified_emails
    df["email_status"] = email_statuses
    df["all_email_results"] = all_results

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    logger.info("=" * 50)
    logger.info("Merge complete")
    logger.info(f"  Total companies: {stats['companies']:,}")
    logger.info(
        f"  With valid email: {stats['with_valid_email']:,} ({100 * stats['with_valid_email'] / max(1, stats['companies']):.0f}%)")
    logger.info(f"  No valid email: {stats['no_valid_email']:,}")
    logger.info(f"  Output: {output_path}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare email variants for NeverBounce and merge results"
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Prepare command
    prepare_parser = subparsers.add_parser("prepare", help="Prepare variants for NeverBounce")
    prepare_parser.add_argument("--input", "-i", required=True, help="Input CSV with email_variants")
    prepare_parser.add_argument("--output", "-o", required=True, help="Output CSV for NeverBounce")

    # Merge command
    merge_parser = subparsers.add_parser("merge", help="Merge NeverBounce results")
    merge_parser.add_argument("--original", required=True, help="Original enriched leads CSV")
    merge_parser.add_argument("--results", "-r", required=True, help="NeverBounce results CSV")
    merge_parser.add_argument("--output", "-o", required=True, help="Final output CSV")

    args = parser.parse_args()

    if args.command == "prepare":
        prepare_for_neverbounce(Path(args.input), Path(args.output))

        print()
        print("Next steps:")
        print("1. Upload to NeverBounce: https://app.neverbounce.com/")
        print("2. Download verified results CSV")
        print(f"3. Run: python -m ... merge --original {args.input} --results <results.csv> --output final_leads.csv")

    elif args.command == "merge":
        merge_neverbounce_results(
            Path(args.original),
            Path(args.results),
            Path(args.output),
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
