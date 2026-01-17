"""
Stage 6b: Email verification with tiered confidence levels.

TIER A - Web Verified (HIGH confidence):
    For each email pattern (first.last@, filast@, fi.last@, first@):
    - Search Google: "email@domain" "Company Name"
    - Search Google: "email@domain" "DirectorLast"
    Only verified if ALL conditions met:
    1. Email appears EXACTLY in snippet/title
    2. Result is from company domain (not data scrapers)
    3. Snippet contains director surname AND (company name OR role word)

TIER B - SMTP Verified (MEDIUM confidence):
    If Tier A fails, use NeverBounce to validate email variants:
    - Send each pattern to NeverBounce API
    - Return first one that's "valid" (deliverable)
    Cost: ~£0.003 per email check

TIER C - Unknown (NO confidence):
    If both Tier A and B fail:
    - Leave email blank
    - No guessing

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage6b_verify_emails \
        --input data/processed/enriched_leads.csv \
        --output data/processed/verified_leads.csv

Requires:
    - GOOGLE_API_KEY in .env
    - GOOGLE_CX in .env
    - NEVERBOUNCE_API_KEY in .env (for Tier B)
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, List

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

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# Data scraper domains to EXCLUDE (their data is guessed/scraped, not primary source)
DATA_SCRAPER_DOMAINS = {
    'zoominfo.com', 'apollo.io', 'rocketreach.co', 'lusha.com',
    'signalhire.com', 'contactout.com', 'hunter.io', 'snov.io',
    'leadiq.com', 'seamless.ai', 'clearbit.com', 'kaspr.io',
    'dropcontact.com', 'voilanorbert.com', 'anymailfinder.com',
    'neverbounce.com', 'emailhunter.co', 'skrapp.io', 'findthatlead.com',
    'uplead.com', 'salesql.com', 'leadfuze.com', 'aeroleads.com',
    'data-lead.com', 'leadiro.com', 'adapt.io',
}

# Business directories to EXCLUDE (often have stale/wrong data)
DIRECTORY_DOMAINS = {
    'endole.co.uk', 'duedil.com', 'companycheck.co.uk', 'companieslist.co.uk',
    'dnb.com', 'crunchbase.com', 'bloomberg.com', 'reuters.com',
    'glassdoor.com', 'indeed.com', 'yell.com', 'yelp.com',
    '192.com', 'spokeo.com', 'whitepages.com', 'yellowbook.com',
}

# Combine all excluded domains
EXCLUDED_DOMAINS = DATA_SCRAPER_DOMAINS | DIRECTORY_DOMAINS

# Role words that suggest the snippet is about the right person
ROLE_WORDS = {
    'director', 'managing', 'ceo', 'chief', 'founder', 'owner',
    'chairman', 'president', 'md', 'partner', 'head',
}

# Email patterns to try (in priority order)
EMAIL_PATTERNS = [
    ("first.last", lambda f, l, d: f"{f}.{l}@{d}"),
    ("filast", lambda f, l, d: f"{f[0]}{l}@{d}"),
    ("fi.last", lambda f, l, d: f"{f[0]}.{l}@{d}"),
    ("first", lambda f, l, d: f"{f}@{d}"),
]


def search_google(query: str, num_results: int = 10) -> List[dict]:
    """Search Google and return results."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return []

    try:
        response = requests.get(
            GOOGLE_SEARCH_URL,
            params={
                "key": GOOGLE_API_KEY,
                "cx": GOOGLE_CX,
                "q": query,
                "num": num_results,
            },
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get("items", []):
            results.append({
                "title": item.get("title", ""),
                "link": item.get("link", ""),
                "snippet": item.get("snippet", ""),
                "displayLink": item.get("displayLink", ""),
            })
        return results

    except Exception as e:
        logger.debug(f"Google search failed: {e}")
        return []


def get_domain_from_url(url: str) -> str:
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except (ValueError, AttributeError):
        return ""


def is_excluded_domain(url: str) -> bool:
    """Check if URL is from an excluded domain."""
    domain = get_domain_from_url(url)
    for excluded in EXCLUDED_DOMAINS:
        if excluded in domain:
            return True
    return False


def is_related_domain(result_domain: str, company_domain: str, company_name: str) -> bool:
    """
    Check if result domain is related to company.

    Related means:
    - Same domain
    - Company name appears in result domain
    - Known related domains (e.g., .co.uk vs .com)
    """
    result_domain = result_domain.lower()
    company_domain = company_domain.lower()

    # Exact match
    if company_domain in result_domain or result_domain in company_domain:
        return True

    # Extract base name from company domain (e.g., "galliard" from "gh.co.uk" won't work,
    # but "galliard" from "galliardhomes.com" would)
    company_base = company_domain.split('.')[0]
    result_base = result_domain.split('.')[0]

    if len(company_base) > 3 and company_base in result_domain:
        return True
    if len(result_base) > 3 and result_base in company_domain:
        return True

    # Check if significant company name word appears in result domain
    clean_name = clean_company_name(company_name).lower()
    name_words = [w for w in clean_name.split() if len(w) > 3]
    for word in name_words:
        if word in result_domain:
            return True

    return False


def clean_company_name(name: str) -> str:
    """Remove legal suffixes for matching."""
    suffixes = [
        ' LIMITED', ' LTD', ' PLC', ' LLP', ' UK', ' GROUP',
        ' HOLDINGS', ' SERVICES', ' SOLUTIONS', ' (UK)',
    ]
    name = name.upper()
    for s in suffixes:
        name = name.replace(s, '')
    return name.strip()


def email_in_text(email: str, text: str) -> bool:
    """Check if email appears exactly in text."""
    return email.lower() in text.lower()


def contains_surname_and_context(text: str, surname: str, company_name: str) -> bool:
    """
    Check if text contains:
    - Director surname, AND
    - Company name OR a role word
    """
    text_lower = text.lower()
    surname_lower = surname.lower()

    # Must contain surname
    if surname_lower not in text_lower:
        return False

    # Must contain company name OR role word
    clean_name = clean_company_name(company_name).lower()
    name_words = [w for w in clean_name.split() if len(w) > 2]

    # Check for company name words
    for word in name_words:
        if word in text_lower:
            return True

    # Check for role words
    for role in ROLE_WORDS:
        if role in text_lower:
            return True

    return False


def verify_email_tier_a(
    email: str,
    company_name: str,
    company_domain: str,
    director_last: str,
) -> Optional[dict]:
    """
    Attempt to verify a single email variant with strict criteria.

    Returns dict with source info if verified, None otherwise.
    """
    clean_name = clean_company_name(company_name)

    # Search 1: "email" "Company Name"
    query1 = f'"{email}" "{clean_name}"'
    results1 = search_google(query1, num_results=5)

    for r in results1:
        # Skip excluded domains
        if is_excluded_domain(r["link"]):
            continue

        combined_text = f"{r['title']} {r['snippet']}"

        # Check all conditions
        if (email_in_text(email, combined_text) and
            is_related_domain(r["displayLink"], company_domain, company_name) and
            contains_surname_and_context(combined_text, director_last, company_name)):

            return {
                "source": r["link"],
                "matched_text": combined_text[:200],
                "search_type": "company_search",
            }

    time.sleep(0.3)

    # Search 2: "email" "DirectorLast"
    query2 = f'"{email}" "{director_last}"'
    results2 = search_google(query2, num_results=5)

    for r in results2:
        # Skip excluded domains
        if is_excluded_domain(r["link"]):
            continue

        combined_text = f"{r['title']} {r['snippet']}"

        # Check all conditions
        if (email_in_text(email, combined_text) and
            is_related_domain(r["displayLink"], company_domain, company_name) and
            contains_surname_and_context(combined_text, director_last, company_name)):

            return {
                "source": r["link"],
                "matched_text": combined_text[:200],
                "search_type": "surname_search",
            }

    return None


def verify_lead(
    director_first: str,
    director_last: str,
    company_name: str,
    domain: str,
) -> dict:
    """
    Verify email for a single lead.

    Tier A: Strict web-verified (email found on company/related site)
    Tier B: Pattern + SMTP verification (verify variants via NeverBounce)
    Tier C: Unknown (leave blank - no reliable data)
    """
    result = {
        "verified_email": "",
        "email_confidence": "",
        "pattern_detected": "",
        "verification_source": "",
        "search_count": 0,
        "smtp_checks": 0,
    }

    if not domain or not director_first or not director_last:
        result["email_confidence"] = "tier_c_unknown"
        return result

    # Clean names for email generation
    first = re.sub(r"['-]", "", director_first.lower().strip())
    last = re.sub(r"['-]", "", director_last.lower().strip())

    if not first or not last:
        result["email_confidence"] = "tier_c_unknown"
        return result

    # =========================================================================
    # TIER A: Strict web-verified
    # =========================================================================
    for pattern_name, pattern_fn in EMAIL_PATTERNS:
        email = pattern_fn(first, last, domain)
        result["search_count"] += 2  # Two searches per pattern

        verification = verify_email_tier_a(
            email=email,
            company_name=company_name,
            company_domain=domain,
            director_last=director_last,
        )

        if verification:
            result["verified_email"] = email
            result["email_confidence"] = "tier_a_verified"
            result["pattern_detected"] = pattern_name
            result["verification_source"] = verification["source"]
            logger.info(f"  ✓ TIER A: {email}")
            logger.info(f"    Source: {verification['source']}")
            return result

        time.sleep(0.3)

    # =========================================================================
    # TIER B: SMTP verification via NeverBounce
    # =========================================================================
    if NEVERBOUNCE_API_KEY:
        tier_b_result = verify_email_tier_b_smtp(
            director_first=first,
            director_last=last,
            domain=domain,
        )
        result["smtp_checks"] += tier_b_result["checks"]

        if tier_b_result["found"]:
            result["verified_email"] = tier_b_result["email"]
            result["email_confidence"] = "tier_b_smtp_valid"
            result["pattern_detected"] = tier_b_result["pattern"]
            result["verification_source"] = "neverbounce_smtp"
            logger.info(f"  → TIER B: {tier_b_result['email']} (SMTP valid)")
            return result
    else:
        logger.debug("  Skipping Tier B - NEVERBOUNCE_API_KEY not set")

    # =========================================================================
    # TIER C: Unknown - no reliable data
    # =========================================================================
    result["email_confidence"] = "tier_c_unknown"
    logger.info(f"  ✗ TIER C: No verified email found")

    return result


# =============================================================================
# TIER B: SMTP VERIFICATION VIA NEVERBOUNCE
# =============================================================================

NEVERBOUNCE_API_KEY = os.environ.get("NEVERBOUNCE_API_KEY", "")
NEVERBOUNCE_API_URL = "https://api.neverbounce.com/v4/single/check"


def verify_email_neverbounce(email: str) -> dict:
    """
    Verify single email via NeverBounce API.

    Returns:
        {
            "valid": True/False,
            "result": "valid" | "invalid" | "disposable" | "catchall" | "unknown",
            "error": None or error message
        }

    NeverBounce result codes:
        valid - Email is valid and deliverable
        invalid - Email is invalid
        disposable - Disposable/temporary email
        catchall - Domain accepts all emails (can't verify)
        unknown - Unable to determine
    """
    if not NEVERBOUNCE_API_KEY:
        return {"valid": False, "result": "no_api_key", "error": "NEVERBOUNCE_API_KEY not set"}

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
        result_str = result_map.get(result_code, "unknown")

        return {
            "valid": result_str == "valid",
            "result": result_str,
            "error": None,
        }

    except requests.exceptions.RequestException as e:
        logger.debug(f"NeverBounce API error for {email}: {e}")
        return {"valid": False, "result": "error", "error": str(e)}


def verify_email_tier_b_smtp(
    director_first: str,
    director_last: str,
    domain: str,
) -> dict:
    """
    Tier B: Verify email variants via SMTP (NeverBounce).

    Tries each pattern and returns the first one that's valid.
    """
    result = {
        "found": False,
        "email": "",
        "pattern": "",
        "checks": 0,
    }

    fi = director_first[0] if director_first else ""

    # Generate variants in priority order (most common UK patterns first)
    variants = [
        (f"{director_first}.{director_last}@{domain}", "first.last"),
        (f"{fi}{director_last}@{domain}", "filast"),
        (f"{fi}.{director_last}@{domain}", "fi.last"),
        (f"{director_first}@{domain}", "first"),
    ]

    for email, pattern in variants:
        result["checks"] += 1

        verification = verify_email_neverbounce(email)

        if verification["valid"]:
            result["found"] = True
            result["email"] = email
            result["pattern"] = pattern
            logger.debug(f"    SMTP valid: {email}")
            return result

        # Small delay between API calls
        time.sleep(0.2)

    return result


def verify_leads(
    input_path: Path,
    output_path: Path,
    limit: Optional[int] = None,
) -> int:
    """Process all leads with Tier A/B/C verification."""
    logger.info(f"Loading leads from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    if limit:
        df = df.head(limit)

    total = len(df)
    logger.info(f"Verifying {total:,} leads")
    logger.info("=" * 50)
    logger.info("Tier A: Web-verified (email on company site)")
    logger.info("Tier B: SMTP-verified (NeverBounce validation)")
    logger.info("Tier C: Unknown (no verification possible)")
    logger.info("=" * 50)

    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.error("GOOGLE_API_KEY and GOOGLE_CX required in .env file")
        return 0

    if not NEVERBOUNCE_API_KEY:
        logger.warning("NEVERBOUNCE_API_KEY not set - Tier B will be skipped")

    # Results
    verified_emails = []
    confidences = []
    patterns = []
    sources = []

    # Stats
    tier_a_count = 0
    tier_b_count = 0
    tier_c_count = 0
    total_searches = 0
    total_smtp_checks = 0

    for i, (_, row) in enumerate(df.iterrows()):
        company_name = row.get("company_name", "")
        logger.info(f"[{i+1}/{total}] {company_name}")

        result = verify_lead(
            director_first=str(row.get("director_first", "")) if pd.notna(row.get("director_first")) else "",
            director_last=str(row.get("director_last", "")) if pd.notna(row.get("director_last")) else "",
            company_name=company_name,
            domain=str(row.get("domain", "")) if pd.notna(row.get("domain")) else "",
        )

        verified_emails.append(result["verified_email"])
        confidences.append(result["email_confidence"])
        patterns.append(result["pattern_detected"])
        sources.append(result["verification_source"])

        # Track stats
        if result["email_confidence"] == "tier_a_verified":
            tier_a_count += 1
        elif result["email_confidence"] == "tier_b_smtp_valid":
            tier_b_count += 1
        else:
            tier_c_count += 1

        total_searches += result["search_count"]
        total_smtp_checks += result.get("smtp_checks", 0)

        time.sleep(0.5)

    # Add columns
    df["verified_email"] = verified_emails
    df["email_confidence"] = confidences
    df["pattern_detected"] = patterns
    df["verification_source"] = sources

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    # Summary
    logger.info("=" * 50)
    logger.info("RESULTS")
    logger.info("=" * 50)
    logger.info(f"Total leads:            {total}")
    logger.info(f"Google searches:        {total_searches}")
    logger.info(f"NeverBounce checks:     {total_smtp_checks}")
    verified_total = tier_a_count + tier_b_count
    logger.info(f"VERIFIED (A+B):         {verified_total:3} ({100*verified_total/total:.0f}%)")
    logger.info(f"Output: {output_path}")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 6b: Tier A strict web-verified director emails"
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