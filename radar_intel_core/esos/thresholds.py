"""
ESOS threshold logic.

Defines the qualification thresholds and provides functions to check
if a company meets ESOS requirements.

ESOS qualification routes:
- Route 1 (EMPLOYEE_TEST): 250+ employees
- Route 2 (FINANCIAL_TEST): £44m+ turnover AND £38m+ balance sheet (BOTH required)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ESOS Thresholds (UK large undertaking definition)
EMPLOYEE_THRESHOLD = 250
TURNOVER_THRESHOLD = 44_000_000  # £44 million
BALANCE_SHEET_THRESHOLD = 38_000_000  # £38 million


@dataclass
class ESOSResult:
    """Result of ESOS threshold check."""
    qualifies: bool
    route: str  # "EMPLOYEE_TEST", "FINANCIAL_TEST", or "NONE"


def meets_esos_threshold(
    employees: Optional[float],
    turnover: Optional[float],
    balance_sheet: Optional[float],
) -> ESOSResult:
    """
    Check if a company meets ESOS thresholds.

    ESOS qualification routes:
    - Route 1: 250+ employees
    - Route 2: £44m+ turnover AND £38m+ balance sheet (BOTH required)

    Args:
        employees: Number of employees (or None if not disclosed)
        turnover: Annual turnover in £ (or None if not disclosed)
        balance_sheet: Balance sheet total in £ (or None if not disclosed)

    Returns:
        ESOSResult with qualifies=True/False and route indicating which test passed
    """
    # Route 1: Employee test
    if employees is not None and employees >= EMPLOYEE_THRESHOLD:
        return ESOSResult(qualifies=True, route="EMPLOYEE_TEST")

    # Route 2: Financial test (BOTH turnover AND balance sheet required)
    if turnover is not None and balance_sheet is not None:
        if turnover >= TURNOVER_THRESHOLD and balance_sheet >= BALANCE_SHEET_THRESHOLD:
            return ESOSResult(qualifies=True, route="FINANCIAL_TEST")

    return ESOSResult(qualifies=False, route="NONE")


def has_sufficient_data(
    employees: Optional[float],
    turnover: Optional[float],
    balance_sheet: Optional[float],
) -> bool:
    """
    Check if we have enough data to make an ESOS determination.

    We need EITHER:
    - Employee count, OR
    - BOTH turnover AND balance sheet

    Args:
        employees: Number of employees (or None)
        turnover: Annual turnover in £ (or None)
        balance_sheet: Balance sheet total in £ (or None)

    Returns:
        True if we have sufficient data for threshold check
    """
    # Can check employee route if we have employee count
    if employees is not None:
        return True

    # Can check financial route if we have BOTH figures
    if turnover is not None and balance_sheet is not None:
        return True

    return False