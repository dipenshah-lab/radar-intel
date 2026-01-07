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

VALIDATION RULES (added to catch filing software bugs):
1. Reject employee counts tagged with currency units (unitRef="GBP" etc.)
2. Reject employee counts with negative scale factors (indicates scaled-up display values)
3. Reject values that look like years (2020-2030 range) - likely table headers
4. Reject implausible balance sheet / employee ratios (< £500 per employee)
5. Reject implausibly high employee counts (> 100,000 for single entity)
6. Reject round numbers that look like salaries (>5000 and divisible by 1000)

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import pandas as pd
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# XBRL CONCEPT DEFINITIONS
# =============================================================================

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


# =============================================================================
# VALIDATION CONSTANTS
# =============================================================================

# Currency codes that indicate monetary values, not counts
# Some filing software incorrectly tags employee counts with GBP
CURRENCY_UNIT_PATTERNS = {"GBP", "USD", "EUR", "ISO4217"}

# Minimum plausible balance sheet per employee (£)
# A company can't realistically have less than ~£500 of net assets per employee
MIN_BALANCE_SHEET_PER_EMPLOYEE = 500

# Maximum plausible employee count for a single UK company filing
# Most large companies file group accounts, so single entity rarely exceeds 50k
# Being conservative at 100k to catch obvious errors while allowing outliers
MAX_PLAUSIBLE_EMPLOYEES = 100_000

# Year range that likely indicates table headers, not employee counts
# e.g., "2025" displayed as a year column header
YEAR_MIN = 2015
YEAR_MAX = 2035

# Threshold above which round numbers (divisible by 1000) are likely salaries
# e.g., 56000 is likely £56,000 salary, not 56,000 employees
SALARY_LIKE_THRESHOLD = 5000


# =============================================================================
# VALIDATION TRACKING
# =============================================================================

@dataclass
class ValidationStats:
    """Track validation rejections for reporting."""
    currency_unit_rejections: int = 0
    negative_scale_rejections: int = 0
    year_value_rejections: int = 0
    ratio_rejections: int = 0
    high_count_rejections: int = 0
    salary_like_rejections: int = 0

    # Store examples for debugging (limit to avoid memory issues)
    currency_unit_examples: list[dict] = field(default_factory=list)
    negative_scale_examples: list[dict] = field(default_factory=list)
    year_value_examples: list[dict] = field(default_factory=list)
    ratio_examples: list[dict] = field(default_factory=list)
    high_count_examples: list[dict] = field(default_factory=list)
    salary_like_examples: list[dict] = field(default_factory=list)

    max_examples: int = 10

    def add_currency_rejection(self, company_number: str, value: float, unit_ref: str):
        self.currency_unit_rejections += 1
        if len(self.currency_unit_examples) < self.max_examples:
            self.currency_unit_examples.append({
                "company_number": company_number,
                "value": value,
                "unit_ref": unit_ref,
            })

    def add_negative_scale_rejection(self, company_number: str, value: float, scale: int):
        self.negative_scale_rejections += 1
        if len(self.negative_scale_examples) < self.max_examples:
            self.negative_scale_examples.append({
                "company_number": company_number,
                "raw_value": value,
                "scale": scale,
                "actual_value": value * (10 ** scale),
            })

    def add_year_value_rejection(self, company_number: str, value: float):
        self.year_value_rejections += 1
        if len(self.year_value_examples) < self.max_examples:
            self.year_value_examples.append({
                "company_number": company_number,
                "value": value,
            })

    def add_ratio_rejection(self, company_number: str, employees: float, balance_sheet: float):
        self.ratio_rejections += 1
        if len(self.ratio_examples) < self.max_examples:
            self.ratio_examples.append({
                "company_number": company_number,
                "employees": employees,
                "balance_sheet": balance_sheet,
                "ratio": balance_sheet / employees if employees > 0 else 0,
            })

    def add_high_count_rejection(self, company_number: str, value: float):
        self.high_count_rejections += 1
        if len(self.high_count_examples) < self.max_examples:
            self.high_count_examples.append({
                "company_number": company_number,
                "value": value,
            })

    def add_salary_like_rejection(self, company_number: str, value: float):
        self.salary_like_rejections += 1
        if len(self.salary_like_examples) < self.max_examples:
            self.salary_like_examples.append({
                "company_number": company_number,
                "value": value,
            })

    def log_summary(self):
        """Log a summary of validation rejections."""
        total = (
            self.currency_unit_rejections +
            self.negative_scale_rejections +
            self.year_value_rejections +
            self.ratio_rejections +
            self.high_count_rejections +
            self.salary_like_rejections
        )

        if total == 0:
            logger.info("Validation: No suspicious values rejected")
            return

        logger.info(f"Validation rejected {total:,} suspicious employee values:")

        if self.currency_unit_rejections > 0:
            logger.info(f"  - Currency unit tagged (GBP/USD/EUR): {self.currency_unit_rejections:,}")
            for ex in self.currency_unit_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had value {ex['value']:,.0f} with unit '{ex['unit_ref']}'")

        if self.negative_scale_rejections > 0:
            logger.info(f"  - Negative scale factor: {self.negative_scale_rejections:,}")
            for ex in self.negative_scale_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had raw value {ex['raw_value']:,.0f} with scale={ex['scale']} (actual: {ex['actual_value']:,.2f})")

        if self.year_value_rejections > 0:
            logger.info(f"  - Year-like value ({YEAR_MIN}-{YEAR_MAX}): {self.year_value_rejections:,}")
            for ex in self.year_value_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had value {ex['value']:,.0f}")

        if self.salary_like_rejections > 0:
            logger.info(f"  - Salary-like round number: {self.salary_like_rejections:,}")
            for ex in self.salary_like_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had value {ex['value']:,.0f}")

        if self.ratio_rejections > 0:
            logger.info(f"  - Implausible balance sheet ratio: {self.ratio_rejections:,}")
            for ex in self.ratio_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had {ex['employees']:,.0f} employees, £{ex['balance_sheet']:,.0f} balance sheet (£{ex['ratio']:,.0f}/employee)")

        if self.high_count_rejections > 0:
            logger.info(f"  - Implausibly high count (>{MAX_PLAUSIBLE_EMPLOYEES:,}): {self.high_count_rejections:,}")
            for ex in self.high_count_examples[:3]:
                logger.info(f"      Example: {ex['company_number']} had value {ex['value']:,.0f}")


# Global validation stats (reset per extraction run)
validation_stats = ValidationStats()


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

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


def is_currency_unit(unit_ref: str) -> bool:
    """
    Check if a unitRef indicates a currency (monetary value).

    Valid employee count units: 'Pure', 'pure', 'Number', custom IDs like 'u4'
    Invalid (currency): 'GBP', 'USD', 'iso4217:GBP', etc.

    Some filing software incorrectly tags employee counts with GBP,
    causing payroll costs to be parsed as headcounts.
    """
    if not unit_ref:
        return False

    unit_upper = unit_ref.upper()

    # Check for any currency pattern
    for pattern in CURRENCY_UNIT_PATTERNS:
        if pattern in unit_upper:
            return True

    return False


def is_year_value(value: float) -> bool:
    """
    Check if a value looks like a year (common in table headers).

    e.g., "2025" used as a column header for the 2025 accounting year
    can be incorrectly tagged as an employee count.
    """
    return YEAR_MIN <= value <= YEAR_MAX


def is_salary_like(value: float) -> bool:
    """
    Check if a value looks like a salary amount rather than employee count.

    High round numbers divisible by 1000 are suspicious when tagged as
    employee counts. e.g., 56000 is more likely £56,000 salary than 56,000 employees.
    """
    if value < SALARY_LIKE_THRESHOLD:
        return False

    # Check if it's a round number (divisible by 1000)
    if value % 1000 == 0:
        return True

    return False


# =============================================================================
# MAIN PARSER
# =============================================================================

def parse_ixbrl_file(content: bytes, track_validation: bool = True) -> Optional[dict[str, Any]]:
    """
    Parse an iXBRL file to extract financial data.

    iXBRL files embed XBRL facts in HTML using ix:nonnumeric and ix:nonfraction tags.
    The 'name' attribute contains the XBRL concept (e.g., "ns5:TurnoverRevenue").

    Note: lxml's HTML parser lowercases all tag names, so we look for
    "ix:nonnumeric" and "ix:nonfraction" (not camelCase).

    IMPORTANT: The 'scale' attribute is only applied to monetary values (turnover,
    balance sheet), NOT to employee counts which are always whole numbers.

    VALIDATION RULES:
    1. Reject employee counts tagged with currency units (unitRef="GBP" etc.)
    2. Reject employee counts with negative scale factors
    3. Reject values that look like years (2020-2030 range)
    4. Reject salary-like round numbers (>5000 and divisible by 1000)
    5. Reject implausible balance sheet / employee ratios (< £500 per employee)
    6. Reject implausibly high employee counts (> 100,000)

    Args:
        content: Raw bytes of the iXBRL file
        track_validation: Whether to track validation stats (disable for testing)

    Returns:
        Dict with extracted data, or None if no valid data found
    """
    global validation_stats

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

        # Track rejected employee value for validation stats
        rejected_employee_value = None
        rejected_employee_unit = None
        rejected_employee_scale = None
        rejection_reason = None

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

                # Get unitRef for validation (lxml lowercases attributes)
                unit_ref = elem.get("unitref", "")

                value = parse_numeric_value(text)

                if value is not None:
                    # Apply sign first (applies to all values)
                    if sign == "-":
                        value = -value

                    # Employees - NEVER apply scale factor
                    # Also validate to catch filing software bugs
                    if concept in EMPLOYEE_CONCEPTS and employees is None:
                        # VALIDATION RULE 1: Reject currency-tagged values
                        if is_currency_unit(unit_ref):
                            logger.debug(
                                f"Rejecting employee count {value:,.0f} - "
                                f"tagged with currency unit '{unit_ref}'"
                            )
                            rejected_employee_value = value
                            rejected_employee_unit = unit_ref
                            rejection_reason = "currency_unit"
                            # Don't set employees - skip this value

                        # VALIDATION RULE 2: Reject negative scale factors
                        # Negative scale means the raw value should be divided
                        # This doesn't make sense for whole-person counts
                        elif scale < 0:
                            logger.debug(
                                f"Rejecting employee count {value:,.0f} - "
                                f"has negative scale={scale} (actual would be {value * (10**scale):,.2f})"
                            )
                            rejected_employee_value = value
                            rejected_employee_scale = scale
                            rejection_reason = "negative_scale"
                            # Don't set employees - skip this value

                        # VALIDATION RULE 3: Reject year-like values
                        elif is_year_value(value):
                            logger.debug(
                                f"Rejecting employee count {value:,.0f} - "
                                f"looks like a year (table header?)"
                            )
                            rejected_employee_value = value
                            rejection_reason = "year_value"
                            # Don't set employees - skip this value

                        # VALIDATION RULE 4: Reject salary-like round numbers
                        elif is_salary_like(value):
                            logger.debug(
                                f"Rejecting employee count {value:,.0f} - "
                                f"looks like salary amount (round number > {SALARY_LIKE_THRESHOLD})"
                            )
                            rejected_employee_value = value
                            rejection_reason = "salary_like"
                            # Don't set employees - skip this value

                        else:
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

        # Track validation rejections
        if track_validation and rejected_employee_value is not None:
            if rejection_reason == "currency_unit":
                validation_stats.add_currency_rejection(
                    company_number, rejected_employee_value, rejected_employee_unit or ""
                )
            elif rejection_reason == "negative_scale":
                validation_stats.add_negative_scale_rejection(
                    company_number, rejected_employee_value, rejected_employee_scale or 0
                )
            elif rejection_reason == "year_value":
                validation_stats.add_year_value_rejection(
                    company_number, rejected_employee_value
                )
            elif rejection_reason == "salary_like":
                validation_stats.add_salary_like_rejection(
                    company_number, rejected_employee_value
                )

        # VALIDATION RULE 5: Reject implausible balance sheet / employee ratios
        if employees is not None and balance_sheet is not None and employees > 0:
            ratio = balance_sheet / employees
            if ratio < MIN_BALANCE_SHEET_PER_EMPLOYEE:
                logger.debug(
                    f"Rejecting {company_number}: balance sheet/employee ratio of £{ratio:,.0f} "
                    f"is implausibly low (employees={employees:,.0f}, balance_sheet=£{balance_sheet:,.0f})"
                )
                if track_validation:
                    validation_stats.add_ratio_rejection(company_number, employees, balance_sheet)
                employees = None  # Clear the suspicious employee value

        # VALIDATION RULE 6: Reject implausibly high employee counts
        if employees is not None and employees > MAX_PLAUSIBLE_EMPLOYEES:
            logger.debug(
                f"Rejecting {company_number}: employee count of {employees:,.0f} is implausibly high"
            )
            if track_validation:
                validation_stats.add_high_count_rejection(company_number, employees)
            employees = None

        # After validation, check again if we have any useful data
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
    global validation_stats

    # Reset validation stats for this run
    validation_stats = ValidationStats()

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

    # Log validation summary
    validation_stats.log_summary()

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
        logger.info("Employee count sanity check (after validation):")
        logger.info(f"  - Min: {emp_series.min():,.0f}")
        logger.info(f"  - Max: {emp_series.max():,.0f}")
        logger.info(f"  - Median: {emp_series.median():,.0f}")
        logger.info(f"  - Companies with 250+ employees: {(emp_series >= 250).sum():,}")

        # Warn if max seems implausibly high (shouldn't happen after validation)
        if emp_series.max() > MAX_PLAUSIBLE_EMPLOYEES:
            logger.warning(
                f"WARNING: Max employee count ({emp_series.max():,.0f}) exceeds plausibility threshold. "
                "Validation may have missed some cases."
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