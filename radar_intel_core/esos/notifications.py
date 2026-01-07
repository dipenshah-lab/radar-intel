"""
ESOS Phase 3 notifications workbook loader.

Loads the Organisation Structure tab to build the exclusion set of
company numbers already covered by ESOS notifications.

The workbook has two key tabs:
- "Responsible Undertaking": 7,627 companies that filed notifications
- "Organisation Structure": 50,876 company numbers covered (RU + all subsidiaries)

We use Organisation Structure because it includes ALL covered companies,
not just those that filed directly.

Column used: "Company registration numbers of relevant undertakings included in the NOC"
"""

from __future__ import annotations

from pathlib import Path
from typing import Set, Dict, Optional
import logging

import pandas as pd


logger = logging.getLogger(__name__)


# Column names in the ESOS Phase 3 workbook
ORGANISATION_STRUCTURE_SHEET = "Organisation Structure"
RESPONSIBLE_UNDERTAKING_SHEET = "Responsible Undertaking"

# The column containing company registration numbers in Organisation Structure
COMPANY_NUMBER_COLUMN = "Company registration numbers of relevant undertakings included in the NOC"

# Columns in Responsible Undertaking sheet (for reference/enrichment)
RU_NAME_COLUMN = "Organisation name"
RU_POSTCODE_COLUMN = "Organisation address - Postcode"
RU_COMPANY_NUMBER_COLUMN = "Company registration number"


class NotificationsData:
    """
    Container for ESOS Phase 3 notifications data.
    
    Attributes:
        covered_numbers: Set of all company numbers covered by notifications (50,876)
        responsible_undertakings: Set of company numbers that filed directly (7,627)
        ru_details: Dict mapping company number to name/postcode for RUs
    """
    
    def __init__(
        self,
        covered_numbers: Set[str],
        responsible_undertakings: Set[str],
        ru_details: Optional[Dict[str, dict]] = None,
    ):
        self.covered_numbers = covered_numbers
        self.responsible_undertakings = responsible_undertakings
        self.ru_details = ru_details or {}
    
    def is_covered(self, company_number: str) -> bool:
        """Check if a company number is covered by any notification."""
        normalised = _normalise_company_number(company_number)
        return normalised in self.covered_numbers
    
    def is_responsible_undertaking(self, company_number: str) -> bool:
        """Check if a company filed a notification directly."""
        normalised = _normalise_company_number(company_number)
        return normalised in self.responsible_undertakings
    
    def __len__(self) -> int:
        """Return count of covered companies."""
        return len(self.covered_numbers)


def _normalise_company_number(number: str) -> str:
    """
    Normalise company number for consistent matching.
    
    - Strip whitespace
    - Uppercase
    - Remove leading zeros (Companies House numbers are 8 chars, often zero-padded)
    """
    if not number or pd.isna(number):
        return ""
    s = str(number).strip().upper()
    # Keep original format - CH numbers can be 8 digits or have letter prefixes
    return s


def load_exclusion_set(workbook_path: Path | str) -> Set[str]:
    """
    Load the set of company numbers covered by ESOS Phase 3 notifications.
    
    This is the main exclusion set used in Stage 3 (find_gaps).
    
    Args:
        workbook_path: Path to ESOS Phase 3 notifications Excel file
    
    Returns:
        Set of normalised company numbers (expected ~50,876)
    
    Raises:
        FileNotFoundError: If workbook doesn't exist
        ValueError: If expected columns are missing
    """
    path = Path(workbook_path)
    if not path.exists():
        raise FileNotFoundError(f"ESOS notifications workbook not found: {path}")
    
    logger.info(f"Loading Organisation Structure from {path}")
    
    try:
        df = pd.read_excel(path, sheet_name=ORGANISATION_STRUCTURE_SHEET)
    except ValueError as e:
        raise ValueError(
            f"Sheet '{ORGANISATION_STRUCTURE_SHEET}' not found in workbook. "
            f"Available sheets can be checked with pd.ExcelFile(path).sheet_names"
        ) from e
    
    if COMPANY_NUMBER_COLUMN not in df.columns:
        raise ValueError(
            f"Column '{COMPANY_NUMBER_COLUMN}' not found in {ORGANISATION_STRUCTURE_SHEET}. "
            f"Available columns: {list(df.columns)}"
        )
    
    # Extract and normalise company numbers
    numbers = (
        df[COMPANY_NUMBER_COLUMN]
        .dropna()
        .astype(str)
        .apply(_normalise_company_number)
    )
    
    exclusion_set = set(numbers)
    
    # Remove empty strings if any
    exclusion_set.discard("")
    
    logger.info(f"Loaded {len(exclusion_set):,} company numbers from Organisation Structure")
    
    return exclusion_set


def load_responsible_undertakings(workbook_path: Path | str) -> Set[str]:
    """
    Load company numbers of Responsible Undertakings (those who filed directly).
    
    This is a subset of the full exclusion set — only the ~7,627 companies
    that submitted notifications, not their covered subsidiaries.
    
    Args:
        workbook_path: Path to ESOS Phase 3 notifications Excel file
    
    Returns:
        Set of normalised company numbers for RUs
    """
    path = Path(workbook_path)
    if not path.exists():
        raise FileNotFoundError(f"ESOS notifications workbook not found: {path}")
    
    logger.info(f"Loading Responsible Undertakings from {path}")
    
    df = pd.read_excel(path, sheet_name=RESPONSIBLE_UNDERTAKING_SHEET)
    
    if RU_COMPANY_NUMBER_COLUMN not in df.columns:
        logger.warning(
            f"Column '{RU_COMPANY_NUMBER_COLUMN}' not found. "
            f"RU company numbers will not be available."
        )
        return set()
    
    numbers = (
        df[RU_COMPANY_NUMBER_COLUMN]
        .dropna()
        .astype(str)
        .apply(_normalise_company_number)
    )
    
    ru_set = set(numbers)
    ru_set.discard("")
    
    logger.info(f"Loaded {len(ru_set):,} Responsible Undertaking company numbers")
    
    return ru_set


def load_notifications_data(workbook_path: Path | str) -> NotificationsData:
    """
    Load complete notifications data including both Organisation Structure
    and Responsible Undertaking details.
    
    Args:
        workbook_path: Path to ESOS Phase 3 notifications Excel file
    
    Returns:
        NotificationsData object with all loaded data
    """
    path = Path(workbook_path)
    
    covered = load_exclusion_set(path)
    rus = load_responsible_undertakings(path)
    
    # Load RU details for enrichment
    ru_details = {}
    try:
        df = pd.read_excel(path, sheet_name=RESPONSIBLE_UNDERTAKING_SHEET)
        
        for _, row in df.iterrows():
            cn = _normalise_company_number(row.get(RU_COMPANY_NUMBER_COLUMN, ""))
            if cn:
                ru_details[cn] = {
                    "name": row.get(RU_NAME_COLUMN),
                    "postcode": row.get(RU_POSTCODE_COLUMN),
                }
    except Exception as e:
        logger.warning(f"Could not load RU details: {e}")
    
    return NotificationsData(
        covered_numbers=covered,
        responsible_undertakings=rus,
        ru_details=ru_details,
    )


def check_coverage(
    company_numbers: list[str],
    exclusion_set: Set[str],
) -> Dict[str, bool]:
    """
    Batch check whether company numbers are in the exclusion set.
    
    Args:
        company_numbers: List of company numbers to check
        exclusion_set: Set of covered company numbers
    
    Returns:
        Dict mapping company_number -> is_covered (bool)
    """
    return {
        cn: _normalise_company_number(cn) in exclusion_set
        for cn in company_numbers
    }
