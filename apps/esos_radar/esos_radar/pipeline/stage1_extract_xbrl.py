"""
Stage 1: Extract financial data from Companies House XBRL bulk files.

Input:  XBRL ZIP file(s) from download.companieshouse.gov.uk
Output: xbrl_extracted.csv with company_number, employees, turnover, balance_sheet

Can either:
1. Parse a local ZIP file (using lxml) - recommended
2. Stream directly from Companies House (using stream-read-xbrl library)

Usage:
    # From local ZIP file:
    python -m apps.esos_radar.esos_radar.pipeline.stage1_extract_xbrl \
        --input data/raw/xbrl/Accounts_Monthly_Data_December2024.zip \
        --output data/processed/xbrl_extracted.csv

    # Stream from Companies House (downloads automatically):
    python -m apps.esos_radar.esos_radar.pipeline.stage1_extract_xbrl \
        --stream \
        --output data/processed/xbrl_extracted.csv \
        --since 2024-01-01
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator, Optional

import pandas as pd
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# XBRL element local names we want to extract (without namespace prefixes)
EMPLOYEE_ELEMENTS = [
    "AverageNumberEmployeesDuringPeriod",
    "EmployeesTotal",
    "NumberOfEmployees",
    "AverageNumberOfEmployeesDuringThePeriod",
]

TURNOVER_ELEMENTS = [
    "TurnoverRevenue",
    "Turnover",
    "TurnoverGrossOperatingRevenue",
    "Revenue",
]

BALANCE_SHEET_ELEMENTS = [
    "BalanceSheetTotalCapitalEmployed",
    "TotalAssetsLessCurrentLiabilities",
    "NetAssetsLiabilities",
    "FixedAssets",
    "TotalAssets",
]

COMPANY_NUMBER_ELEMENTS = [
    "UKCompaniesHouseRegisteredNumber",
    "CompaniesHouseRegisteredNumber",
]

COMPANY_NAME_ELEMENTS = [
    "EntityCurrentLegalOrRegisteredName",
]

PERIOD_END_ELEMENTS = [
    "BalanceSheetDate",
    "PeriodEnd",
    "EndDateForPeriodCoveredByReport",
]


def extract_value_by_localname(
    root: etree._Element,  # noqa: SLF001
    element_names: list[str],
) -> Optional[str]:
    """
    Extract text value from first matching element by local name.

    Args:
        root: lxml element tree root
        element_names: List of local names to search for (priority order)

    Returns:
        Text content or None
    """
    for elem_name in element_names:
        # Search for element with matching local name (ignoring namespace)
        for elem in root.iter():
            local_name = etree.QName(elem.tag).localname if "}" in elem.tag else elem.tag
            if local_name == elem_name and elem.text:
                return elem.text.strip()
    return None


def extract_numeric_value(
    root: etree._Element,  # noqa: SLF001
    element_names: list[str],
) -> Optional[float]:
    """Extract numeric value from first matching element."""
    text = extract_value_by_localname(root, element_names)
    if text:
        try:
            # Remove commas and convert
            return float(text.replace(",", ""))
        except ValueError:
            return None
    return None


def extract_company_number(
    root: etree._Element,  # noqa: SLF001
) -> Optional[str]:
    """Extract and normalize company number."""
    text = extract_value_by_localname(root, COMPANY_NUMBER_ELEMENTS)
    if text:
        cn = text.strip().upper()
        # Valid UK company numbers: 8 alphanumeric chars
        cn_clean = re.sub(r"[^A-Z0-9]", "", cn)
        if len(cn_clean) <= 8:
            return cn_clean.zfill(8)
    return None


def parse_single_xbrl_lxml(content: bytes) -> Optional[dict[str, Any]]:
    """
    Parse a single iXBRL/XBRL file using lxml.

    Args:
        content: Raw bytes of XBRL file

    Returns:
        Dict with extracted fields or None if parsing fails
    """
    try:
        # Parse as HTML (iXBRL) or XML
        try:
            root = etree.fromstring(content)
        except etree.XMLSyntaxError:
            # Try HTML parser for iXBRL
            root = etree.HTML(content)

        if root is None:
            return None

        company_number = extract_company_number(root)
        if not company_number:
            return None

        employees = extract_numeric_value(root, EMPLOYEE_ELEMENTS)
        turnover = extract_numeric_value(root, TURNOVER_ELEMENTS)
        balance_sheet = extract_numeric_value(root, BALANCE_SHEET_ELEMENTS)

        # Skip if no useful financial data
        if employees is None and turnover is None and balance_sheet is None:
            return None

        company_name = extract_value_by_localname(root, COMPANY_NAME_ELEMENTS)
        accounts_date = extract_value_by_localname(root, PERIOD_END_ELEMENTS)

        # Clean up accounts date
        if accounts_date:
            accounts_date = accounts_date[:10]  # YYYY-MM-DD

        return {
            "company_number": company_number,
            "company_name": company_name,
            "employees": int(employees) if employees is not None else None,
            "turnover": turnover,
            "balance_sheet": balance_sheet,
            "accounts_date": accounts_date,
        }

    except Exception as e:
        logger.debug(f"Failed to parse XBRL: {e}")
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
            if name.lower().endswith((".html", ".xhtml", ".xml")):
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
    skipped = 0

    for filename, content in iter_xbrl_from_zip(zip_path):
        processed += 1

        if limit and processed > limit:
            break

        if processed % 1000 == 0:
            logger.info(f"Processed {processed:,} files, extracted {extracted:,} records")

        result = parse_single_xbrl_lxml(content)

        if result:
            records.append(result)
            extracted += 1
        else:
            skipped += 1

    logger.info(f"Completed: {processed:,} files, {extracted:,} extracted, {skipped:,} skipped")

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
    has_employees = df["employees"].notna().sum()
    has_turnover = df["turnover"].notna().sum()
    has_balance = df["balance_sheet"].notna().sum()

    logger.info("Data availability in output:")
    logger.info(f"  - Employees: {has_employees:,} ({100*has_employees/len(df):.1f}%)")
    logger.info(f"  - Turnover: {has_turnover:,} ({100*has_turnover/len(df):.1f}%)")
    logger.info(f"  - Balance sheet: {has_balance:,} ({100*has_balance/len(df):.1f}%)")

    return len(df)


def extract_from_stream(
    output_path: Path,
    since_date: Optional[date] = None,
    limit: Optional[int] = None,
) -> int:
    """
    Extract financial data by streaming from Companies House.

    Uses stream-read-xbrl library to download and parse directly.

    Args:
        output_path: Path for output CSV
        since_date: Only process filings after this date
        limit: Optional limit on records to process

    Returns:
        Number of companies extracted
    """
    try:
        from stream_read_xbrl import stream_read_xbrl_sync
    except ImportError:
        logger.error(
            "stream-read-xbrl not installed. Install with: pip install stream-read-xbrl"
        )
        sys.exit(1)

    logger.info("Streaming from Companies House...")
    if since_date:
        logger.info(f"Processing filings since: {since_date}")

    records: list[dict[str, Any]] = []
    processed = 0

    ingest_date = since_date or date(2024, 1, 1)

    for row in stream_read_xbrl_sync(ingest_data_after_date=ingest_date):
        processed += 1

        if limit and processed > limit:
            break

        if processed % 1000 == 0:
            logger.info(f"Processed {processed:,} records")

        # stream_read_xbrl_sync yields dicts with standardized column names
        company_number = row.get("company_number")
        if not company_number:
            continue

        employees = row.get("employees_average") or row.get("employees")
        turnover = row.get("turnover")
        balance_sheet = row.get("net_assets") or row.get("total_assets")

        # Skip if no useful data
        if employees is None and turnover is None and balance_sheet is None:
            continue

        records.append({
            "company_number": str(company_number).zfill(8),
            "company_name": row.get("company_name"),
            "employees": int(employees) if employees else None,
            "turnover": float(turnover) if turnover else None,
            "balance_sheet": float(balance_sheet) if balance_sheet else None,
            "accounts_date": row.get("period_end"),
        })

    logger.info(f"Completed: {processed:,} processed, {len(records):,} extracted")

    if not records:
        logger.warning("No records extracted!")
        return 0

    df = pd.DataFrame(records)

    # Deduplicate
    df = df.sort_values("accounts_date", ascending=False, na_position="last")
    df = df.drop_duplicates(subset=["company_number"], keep="first")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    logger.info(f"Wrote {len(df):,} unique companies to {output_path}")

    return len(df)


def main() -> None:
    """Main entry point for CLI."""
    parser = argparse.ArgumentParser(
        description="Extract financial data from Companies House XBRL"
    )
    parser.add_argument(
        "--input",
        "-i",
        help="Path to local XBRL ZIP file",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path for output CSV",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream from Companies House instead of local file",
    )
    parser.add_argument(
        "--since",
        help="For streaming: only process filings after this date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Limit number of files/records to process (for testing)",
    )

    args = parser.parse_args()

    output_path = Path(args.output)

    if args.stream:
        since_date_val = None
        if args.since:
            since_date_val = datetime.strptime(args.since, "%Y-%m-%d").date()
        count = extract_from_stream(output_path, since_date_val, args.limit)
    else:
        if not args.input:
            logger.error("Either --input or --stream is required")
            sys.exit(1)

        zip_path = Path(args.input)
        if not zip_path.exists():
            logger.error(f"Input file not found: {zip_path}")
            sys.exit(1)

        count = extract_from_zip(zip_path, output_path, args.limit)

    if count == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
