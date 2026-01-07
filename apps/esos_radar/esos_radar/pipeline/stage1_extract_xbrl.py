"""
Stage 1: Extract financial data from Companies House XBRL bulk files.

Input:  XBRL ZIP file(s) from download.companieshouse.gov.uk
Output: xbrl_extracted.csv with company_number, employees, turnover, balance_sheet

The Companies House iXBRL files use inline XBRL format where:
- ix:nonnumeric contains text values (company number, name, dates)
- ix:nonfraction contains numeric values (employees, turnover, balance sheet)
- The 'name' attribute contains the XBRL concept (e.g., "ns5:TotalAssetsLessCurrentLiabilities")

Note: When parsed with lxml's HTML parser, tag names are lowercased.

IMPORTANT: The 'scale' attribute (e.g., scale="3" for thousands) should ONLY be applied
to monetary values (turnover, balance sheet), NOT to employee counts which are always
reported as whole numbers.

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage1_extract_xbrl \
        --input data/raw/xbrl/Accounts_Monthly_Data-December2024.zip \
        --output data/processed/xbrl_extracted.csv
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional

import pandas as pd
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# XBRL concept names (the part after the namespace prefix)
# These appear in the 'name' attribute like "ns5:AverageNumberEmployeesDuringPeriod"

EMPLOYEE_CONCEPTS = [
    "AverageNumberEmployeesDuringPeriod",
    "EmployeesTotal",
    "NumberOfEmployees",
    "AverageNumberOfEmployeesDuringThePeriod",
]

TURNOVER_CONCEPTS = [
    "TurnoverRevenue",
    "Turnover",
    "TurnoverGrossOperatingRevenue",
    "Revenue",
]

BALANCE_SHEET_CONCEPTS = [
    "TotalAssetsLessCurrentLiabilities",
    "NetAssetsLiabilities",
    "FixedAssets",
    "TotalAssets",
]

COMPANY_NUMBER_CONCEPTS = [
    "UKCompaniesHouseRegisteredNumber",
    "CompaniesHouseRegisteredNumber",
]

COMPANY_NAME_CONCEPTS = [
    "EntityCurrentLegalOrRegisteredName",
]

PERIOD_END_CONCEPTS = [
    "BalanceSheetDate",
    "EndDateForPeriodCoveredByReport",
]


def get_concept_name(full_name: str) -> str:
    """Extract concept name from full name like 'ns5:AverageNumberEmployeesDuringPeriod'."""
    if ":" in full_name:
        return full_name.split(":")[-1]
    return full_name


def parse_numeric_value(text: str) -> Optional[float]:
    """Parse a numeric value from iXBRL text, handling commas and formatting."""
    if not text:
        return None
    # Remove commas and whitespace
    cleaned = text.replace(",", "").replace(" ", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_ixbrl_file(content: bytes) -> Optional[dict[str, Any]]:
    """
    Parse an iXBRL file to extract financial data.

    iXBRL files embed XBRL facts in HTML using ix:nonnumeric and ix:nonfraction tags.
    The 'name' attribute contains the XBRL concept (e.g., "ns5:TurnoverRevenue").

    Note: lxml's HTML parser lowercases all tag names, so we look for
    "ix:nonnumeric" and "ix:nonfraction" (not camelCase).

    IMPORTANT: The 'scale' attribute is only applied to monetary values (turnover,
    balance sheet), NOT to employee counts which are always whole numbers.
    """
    try:
        # Parse as HTML since iXBRL is HTML-based
        root = etree.HTML(content)
        if root is None:
            return None

        company_number = None
        company_name = None
        employees = None
        turnover = None
        balance_sheet = None
        accounts_date = None

        # Iterate through all elements
        for elem in root.iter():
            # Get the tag - lxml HTML parser lowercases tags
            tag = str(elem.tag)

            # Handle ix:nonnumeric elements (text values like company number)
            if tag == "ix:nonnumeric":
                name_attr = elem.get("name", "")
                concept = get_concept_name(name_attr)

                # Get text content (may be nested in child elements)
                text = elem.text or ""
                if not text.strip():
                    text = "".join(elem.itertext())
                text = text.strip()

                # Company number
                if concept in COMPANY_NUMBER_CONCEPTS and text:
                    # Normalize company number
                    cn = re.sub(r"[^A-Z0-9]", "", text.upper())
                    if len(cn) <= 8:
                        company_number = cn.zfill(8)

                # Company name
                elif concept in COMPANY_NAME_CONCEPTS and text:
                    company_name = text

                # Period end date
                elif concept in PERIOD_END_CONCEPTS and text:
                    # Try to extract date in format YYYY-MM-DD
                    if len(text) >= 10:
                        accounts_date = text[:10]

            # Handle ix:nonfraction elements (numeric values)
            elif tag == "ix:nonfraction":
                name_attr = elem.get("name", "")
                concept = get_concept_name(name_attr)

                # Get text content
                text = elem.text or ""
                if not text.strip():
                    text = "".join(elem.itertext())
                text = text.strip()

                # Check for scale attribute (e.g., scale="3" means thousands)
                # NOTE: Only applied to MONETARY values, not employee counts
                scale = int(elem.get("scale", "0"))

                # Check for sign attribute (negative values)
                sign = elem.get("sign", "")

                value = parse_numeric_value(text)

                if value is not None:
                    # Apply sign first (applies to all values)
                    if sign == "-":
                        value = -value

                    # Employees - NEVER apply scale factor
                    # Employee counts are always reported as whole numbers
                    if concept in EMPLOYEE_CONCEPTS and employees is None:
                        employees = value  # No scaling!

                    # Turnover - apply scale (monetary values may use thousands/millions)
                    elif concept in TURNOVER_CONCEPTS and turnover is None:
                        turnover = value * (10 ** scale)

                    # Balance sheet - apply scale (monetary values may use thousands/millions)
                    elif concept in BALANCE_SHEET_CONCEPTS and balance_sheet is None:
                        balance_sheet = value * (10 ** scale)

        # Must have company number
        if not company_number:
            return None

        # Must have at least some financial data
        if employees is None and turnover is None and balance_sheet is None:
            return None

        return {
            "company_number": company_number,
            "company_name": company_name,
            "employees": int(employees) if employees is not None else None,
            "turnover": turnover,
            "balance_sheet": balance_sheet,
            "accounts_date": accounts_date,
        }

    except Exception as e:
        logger.debug(f"Failed to parse iXBRL: {e}")
        return None


def iter_xbrl_from_zip(zip_path: Path) -> Iterator[tuple[str, bytes]]:
    """
    Iterate over iXBRL files in a Companies House ZIP.

    Args:
        zip_path: Path to ZIP file

    Yields:
        Tuples of (filename, file_content_bytes)
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        for name in zf.namelist():
            # iXBRL files have .html extension in CH data
            if name.lower().endswith((".html", ".xhtml")):
                try:
                    content = zf.read(name)
                    yield name, content
                except Exception as e:
                    logger.warning(f"Failed to read {name}: {e}")


def extract_from_zip(
    zip_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
) -> int:
    """
    Extract financial data from all iXBRL files in a local ZIP.

    Args:
        zip_path: Path to Companies House XBRL ZIP
        output_path: Path for output CSV
        limit: Optional limit on number of files to process (for testing)

    Returns:
        Number of companies successfully extracted
    """
    logger.info(f"Processing local ZIP: {zip_path}")

    records: list[dict[str, Any]] = []
    processed = 0
    extracted = 0
    skipped_no_data = 0
    skipped_no_company = 0

    for filename, content in iter_xbrl_from_zip(zip_path):
        processed += 1

        if limit and processed > limit:
            break

        if processed % 1000 == 0:
            logger.info(f"Processed {processed:,} files, extracted {extracted:,} records")

        result = parse_ixbrl_file(content)

        if result:
            records.append(result)
            extracted += 1
        else:
            # Track why we skipped
            # Quick check if it has company number
            if b"UKCompaniesHouseRegisteredNumber" in content or b"CompaniesHouseRegisteredNumber" in content:
                skipped_no_data += 1
            else:
                skipped_no_company += 1

    logger.info(f"Completed: {processed:,} files processed")
    logger.info(f"  - Extracted: {extracted:,}")
    logger.info(f"  - Skipped (no financial data): {skipped_no_data:,}")
    logger.info(f"  - Skipped (no company number): {skipped_no_company:,}")

    if not records:
        logger.warning("No records extracted!")
        return 0

    df = pd.DataFrame(records)

    # Deduplicate by company number, keeping most recent
    df = df.sort_values("accounts_date", ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["company_number"], keep="first")

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(df):,} unique companies to {output_path}")

    # Log some stats
    has_employees = int(df["employees"].notna().sum())
    has_turnover = int(df["turnover"].notna().sum())
    has_balance = int(df["balance_sheet"].notna().sum())

    logger.info("Data availability in output:")
    logger.info(f"  - Employees: {has_employees:,} ({100*has_employees/len(df):.1f}%)")
    logger.info(f"  - Turnover: {has_turnover:,} ({100*has_turnover/len(df):.1f}%)")
    logger.info(f"  - Balance sheet: {has_balance:,} ({100*has_balance/len(df):.1f}%)")

    # Log sanity check for employee counts
    if has_employees > 0:
        emp_series = df["employees"].dropna()
        logger.info("Employee count sanity check:")
        logger.info(f"  - Min: {emp_series.min():,.0f}")
        logger.info(f"  - Max: {emp_series.max():,.0f}")
        logger.info(f"  - Median: {emp_series.median():,.0f}")
        logger.info(f"  - Companies with 250+ employees: {(emp_series >= 250).sum():,}")

        # Warn if max seems implausibly high
        if emp_series.max() > 500_000:
            logger.warning(
                f"WARNING: Max employee count ({emp_series.max():,.0f}) seems implausibly high. "
                "Check for scaling issues in source data."
            )

    return len(df)


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Extract financial data from Companies House XBRL"
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to local XBRL ZIP file",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path for output CSV",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Limit number of files to process (for testing)",
    )

    args = parser.parse_args()

    zip_path = Path(args.input)
    output_path = Path(args.output)

    if not zip_path.exists():
        logger.error(f"Input file not found: {zip_path}")
        sys.exit(1)

    count = extract_from_zip(zip_path, output_path, args.limit)

    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()