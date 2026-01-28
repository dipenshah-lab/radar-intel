"""
Stage 6b: Email verification via SMTP (NeverBounce).

Directly verifies email variants via NeverBounce SMTP checking.
No Google search (proven 0% hit rate for UK SMEs).

For each lead:
  - Generate 4 email variants (first.last@, filast@, fi.last@, first@)
  - Send each to NeverBounce API
  - Return first one that's "valid" (deliverable)
  - Cost: ~£0.003 per check, max £0.012 per lead

Output confidence levels:
  - smtp_valid: NeverBounce confirmed deliverable
  - smtp_catchall: Domain accepts all emails (can't verify, but likely works)
  - smtp_unknown: Could not determine (might work)
  - no_valid_email: All variants invalid or no domain/director

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage6b_verify_emails \
        --input data/processed/enriched_leads.csv \
        --output data/processed/verified_leads.csv

Requires:
    - NEVERBOUNCE_API_KEY in .env
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# CONFIGURATION
# =============================================================================

NEVERBOUNCE_API_KEY = os.environ.get("NEVERBOUNCE_API_KEY", "")
NEVERBOUNCE_API_URL = "https://api.neverbounce.com/v4/single/check"

# Email patterns to try (in priority order - most common UK formats first)
EMAIL_PATTERNS = [
    ("first.last", lambda f, l, d: f"{f}.{l}@{d}"),
    ("filast", lambda f, l, d: f"{f[0]}{l}@{d}"),
    ("fi.last", lambda f, l, d: f"{f[0]}.{l}@{d}"),
    ("first", lambda f, l, d: f"{f}@{d}"),
]


# =============================================================================
# NEVERBOUNCE SMTP VERIFICATION
# =============================================================================

def verify_email_neverbounce(email: str) -> dict:
    """
    Verify single email via NeverBounce API.

    Returns:
        {
            "status": "valid" | "invalid" | "disposable" | "catchall" | "unknown",
            "error": None or error message
        }

    NeverBounce result codes:
        0/valid - Email is valid and deliverable
        1/invalid - Email is invalid
        2/disposable - Disposable/temporary email
        3/catchall - Domain accepts all emails (can't verify individual)
        4/unknown - Unable to determine
    """
    if not NEVERBOUNCE_API_KEY:
        return {"status": "error", "error": "NEVERBOUNCE_API_KEY not set"}

    try:
        response = requests.get(
            NEVERBOUNCE_API_URL,
            params={
                "key": NEVERBOUNCE_API_KEY,
                "email": email,
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # NeverBounce returns result as integer or string
        result_map = {
            0: "valid",
            1: "invalid",
            2: "disposable",
            3: "catchall",
            4: "unknown",
            "valid": "valid",
            "invalid": "invalid",
            "disposable": "disposable",
            "catchall": "catchall",
            "unknown": "unknown",
        }

        result_code = data.get("result")
        status = result_map.get(result_code, "unknown")

        return {"status": status, "error": None}

    except requests.exceptions.RequestException as e:
        logger.debug(f"NeverBounce API error for {email}: {e}")
        return {"status": "error", "error": str(e)}


def verify_lead(
    director_first: str,
    director_last: str,
    domain: str,
) -> dict:
    """
    Verify email for a single lead via NeverBounce SMTP.

    Tries each email pattern until one is valid.
    Also accepts catchall domains (likely to work).
    """
    result = {
        "verified_email": "",
        "email_confidence": "",
        "pattern_detected": "",
        "smtp_checks": 0,
        "all_results": [],  # Track all results for debugging
    }

    if not domain or not director_first or not director_last:
        result["email_confidence"] = "no_valid_email"
        return result

    # Clean names for email generation
    first = re.sub(r"['-]", "", director_first.lower().strip())
    last = re.sub(r"['-]", "", director_last.lower().strip())

    if not first or not last:
        result["email_confidence"] = "no_valid_email"
        return result

    # Track catchall result in case no valid found
    catchall_email = None
    catchall_pattern = None

    # Try each email pattern
    for pattern_name, pattern_fn in EMAIL_PATTERNS:
        email = pattern_fn(first, last, domain)
        result["smtp_checks"] += 1

        verification = verify_email_neverbounce(email)
        status = verification["status"]

        result["all_results"].append({"email": email, "status": status})

        if status == "valid":
            result["verified_email"] = email
            result["email_confidence"] = "smtp_valid"
            result["pattern_detected"] = pattern_name
            logger.info(f"  ✓ VALID: {email}")
            return result

        elif status == "catchall" and not catchall_email:
            # Save first catchall as fallback
            catchall_email = email
            catchall_pattern = pattern_name
            logger.debug(f"    Catchall: {email}")

        elif status == "invalid":
            logger.debug(f"    Invalid: {email}")

        # Small delay between API calls
        time.sleep(0.2)

    # No valid found - use catchall if available
    if catchall_email:
        result["verified_email"] = catchall_email
        result["email_confidence"] = "smtp_catchall"
        result["pattern_detected"] = catchall_pattern
        logger.info(f"  → CATCHALL: {catchall_email} (domain accepts all)")
        return result

    # Nothing worked
    result["email_confidence"] = "no_valid_email"
    logger.info(f"  ✗ No valid email found")

    return result


# =============================================================================
# MAIN VERIFICATION FUNCTION
# =============================================================================

def verify_leads(
    input_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
) -> int:
    """Process all leads with NeverBounce SMTP verification."""
    logger.info(f"Loading leads from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    # Filter to leads with verified domains and directors
    has_domain = df["domain"].notna() & (df["domain"] != "")
    has_director = df["director_first"].notna() & (df["director_first"] != "")

    df_to_process = df[has_domain & has_director].copy()
    skipped = len(df) - len(df_to_process)

    if limit:
        df_to_process = df_to_process.head(limit)

    total = len(df_to_process)
    logger.info(f"Verifying {total:,} leads ({skipped} skipped - no domain or director)")
    logger.info("=" * 50)

    if not NEVERBOUNCE_API_KEY:
        logger.error("NEVERBOUNCE_API_KEY required in .env file")
        logger.error("Get your key at: https://neverbounce.com")
        return 0

    # Results
    verified_emails = []
    confidences = []
    patterns = []

    # Stats
    valid_count = 0
    catchall_count = 0
    no_email_count = 0
    total_smtp_checks = 0

    for i, (idx, row) in enumerate(df_to_process.iterrows()):
        company_name = row.get("company_name", "")

        if (i + 1) % 10 == 0 or (i + 1) == total:
            logger.info(f"Progress: {i+1}/{total} ({100*(i+1)/total:.0f}%)")
        else:
            logger.info(f"[{i+1}/{total}] {company_name}")

        result = verify_lead(
            director_first=str(row.get("director_first", "")) if pd.notna(row.get("director_first")) else "",
            director_last=str(row.get("director_last", "")) if pd.notna(row.get("director_last")) else "",
            domain=str(row.get("domain", "")) if pd.notna(row.get("domain")) else "",
        )

        verified_emails.append(result["verified_email"])
        confidences.append(result["email_confidence"])
        patterns.append(result["pattern_detected"])

        # Track stats
        if result["email_confidence"] == "smtp_valid":
            valid_count += 1
        elif result["email_confidence"] == "smtp_catchall":
            catchall_count += 1
        else:
            no_email_count += 1

        total_smtp_checks += result["smtp_checks"]

        time.sleep(0.3)  # Rate limit

    # Add columns to processed dataframe
    df_to_process["verified_email"] = verified_emails
    df_to_process["email_confidence"] = confidences
    df_to_process["email_pattern"] = patterns

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df_to_process.to_csv(output_path, index=False)

    # Summary
    logger.info("=" * 50)
    logger.info("RESULTS")
    logger.info("=" * 50)
    logger.info(f"Total leads processed:  {total}")
    logger.info(f"NeverBounce API calls:  {total_smtp_checks}")
    logger.info(f"Estimated cost:         £{total_smtp_checks * 0.003:.2f}")
    logger.info(f"VALID (deliverable):    {valid_count:3} ({100*valid_count/total:.0f}%)")
    logger.info(f"CATCHALL (likely ok):   {catchall_count:3} ({100*catchall_count/total:.0f}%)")
    logger.info(f"NO EMAIL FOUND:         {no_email_count:3} ({100*no_email_count/total:.0f}%)")
    logger.info("-" * 30)
    usable = valid_count + catchall_count
    logger.info(f"USABLE EMAILS:          {usable:3} ({100*usable/total:.0f}%)")
    logger.info(f"Output: {output_path}")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 6b: Verify director emails via NeverBounce SMTP"
    )
    parser.add_argument("--input", "-i", required=True, help="Input CSV from Stage 6")
    parser.add_argument("--output", "-o", required=True, help="Output CSV")
    parser.add_argument("--limit", "-n", type=int, help="Limit leads (for testing)")

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        logger.error(f"Input not found: {input_path}")
        return

    verify_leads(input_path, output_path, limit=args.limit)


if __name__ == "__main__":
    main()