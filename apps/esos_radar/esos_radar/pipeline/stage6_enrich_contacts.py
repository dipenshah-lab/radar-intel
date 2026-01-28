"""
Stage 6: Enrich leads with domain and director contact information.

Input:  tier_a_plus_leads.csv (from Stage 5)
Output: enriched_leads.csv (with domains, directors, email variants)

Four-step process:
1. Find company domains via DNS brute-force (or Google fallback)
2. VERIFY domain matches company (fetch homepage, check for name/address)
3. Extract director names from Companies House API
4. Generate email variants for verification

Usage:
    python -m apps.esos_radar.esos_radar.pipeline.stage6_enrich_contacts \
        --input data/processed/tier_a_plus_leads.csv \
        --output data/processed/enriched_leads.csv

Or with Google fallback for missing domains:
    python -m apps.esos_radar.esos_radar.pipeline.stage6_enrich_contacts \
        --input data/processed/tier_a_plus_leads.csv \
        --output data/processed/enriched_leads.csv \
        --google-fallback
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.parse import urlparse
import os

import pandas as pd
import requests

 #from radar_intel_core.clients.ch_client import CompaniesHouseClient
from radar_intel_core.clients.ch_cache import CachedCompaniesHouseClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# DOMAIN FINDING - MULTI-STRATEGY APPROACH
# =============================================================================

# Extended domain patterns to try
DOMAIN_PATTERNS = [
    # Primary UK patterns
    "{name}.co.uk",
    "{name}.com",
    "{name}.uk",
    # Secondary patterns
    "{name}.org.uk",
    "{name}.net",
    "{name}.org",
    "{name}.eu",
    "{name}.io",
    # UK-specific variations
    "{name}uk.com",
    "{name}-uk.com",
    "{name}ltd.co.uk",
    "{name}group.co.uk",
    "{name}group.com",
    "{name}holdings.com",
    "{name}holdings.co.uk",
    "{name}online.co.uk",
    "{name}online.com",
]

# Legal suffixes to strip from company names
LEGAL_SUFFIXES = [
    "limited", "ltd", "plc", "llp", "lp", "inc", "corp", "corporation",
    "uk", "holdings", "group", "international", "services", "solutions",
    "consulting", "consultants", "associates", "partners", "partnership",
    "enterprises", "industries", "manufacturing", "engineering",
]

# Words to remove that often aren't in domains
NOISE_WORDS = ["the", "and", "&", "of", "for"]


def clean_name_for_domain(name: str) -> str:
    """Clean company name to create domain-friendly slug."""
    name = name.lower()

    # Remove legal suffixes
    for suffix in LEGAL_SUFFIXES:
        name = re.sub(rf'\b{suffix}\b\.?', '', name)

    # Remove noise words
    for word in NOISE_WORDS:
        name = re.sub(rf'\b{word}\b', '', name)

    # Remove punctuation, keep alphanumeric and spaces
    name = re.sub(r'[^\w\s]', '', name)

    # Collapse whitespace and convert to slug
    name = '-'.join(name.split())

    return name.strip('-')


def get_name_variations(name: str) -> List[str]:
    """
    Generate multiple slug variations to maximise DNS hit rate.

    E.g. "Acme Industrial Pumps Limited" might have domain:
    - acme-industrial-pumps.co.uk (full name)
    - acmeindustrialpumps.co.uk (no hyphens)
    - acme.co.uk (first word only - common!)
    - acme-industrial.co.uk (first two words)
    - aip.co.uk (initials)
    """
    base = clean_name_for_domain(name)

    if not base:
        return []

    words = base.split('-')
    no_hyphens = base.replace('-', '')

    # Build variations list
    variations = [base]  # 1. Full name with hyphens

    # 2. Full name without hyphens
    if no_hyphens != base:
        variations.append(no_hyphens)

    # 3. First word only (often the brand name)
    if len(words) >= 1 and len(words[0]) >= 3:
        variations.append(words[0])

    # 4. First two words (with and without hyphen)
    if len(words) >= 2:
        first_two = '-'.join(words[:2])
        variations.append(first_two)
        variations.append(first_two.replace('-', ''))

    # 5. First three words
    if len(words) >= 3:
        first_three = '-'.join(words[:3])
        variations.append(first_three)
        variations.append(first_three.replace('-', ''))

    # 6. Initials (for longer names)
    if len(words) >= 2:
        initials = ''.join(w[0] for w in words if w)
        if len(initials) >= 2:
            variations.append(initials)

    # 7. First word + initials of rest
    if len(words) >= 2:
        first_plus_initials = words[0] + ''.join(w[0] for w in words[1:] if w)
        variations.append(first_plus_initials)

    # Remove duplicates while preserving order
    seen = set()
    unique = []
    for v in variations:
        if v and v not in seen:
            seen.add(v)
            unique.append(v)

    return unique


def check_domain_exists(domain: str, timeout: float = 2.0) -> bool:
    """Check if domain resolves via DNS."""
    try:
        socket.setdefaulttimeout(timeout)
        socket.gethostbyname(domain)
        return True
    except (socket.gaierror, socket.timeout):
        return False


def extract_domain_from_url(url: str) -> Optional[str]:
    """Extract clean domain from URL."""
    if not url:
        return None

    # Add scheme if missing
    schemes = ('http', 'https')
    has_scheme = any(url.startswith(f'{s}://') for s in schemes)
    if not has_scheme:
        url = 'https://' + url

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Remove www prefix
        if domain.startswith('www.'):
            domain = domain[4:]

        # Remove port if present
        if ':' in domain:
            domain = domain.split(':')[0]

        return domain if domain else None
    except (ValueError, AttributeError):
        return None


def get_domain_from_ch_api(client: CachedCompaniesHouseClient, company_number: str) -> Optional[str]:
    """
    Check Companies House API for website/links on file.

    Some companies have their website recorded in the API response.
    This is the most reliable source when available.
    """
    try:
        profile = client.get_company(company_number)

        # Check direct website field (rare but exists)
        if profile.get("website"):
            domain = extract_domain_from_url(profile["website"])
            if domain:
                return domain

        # Check links section
        links = profile.get("links", {})
        if links.get("website"):
            domain = extract_domain_from_url(links["website"])
            if domain:
                return domain

        # Some profiles have it in a different location
        if profile.get("company_url"):
            domain = extract_domain_from_url(profile["company_url"])
            if domain:
                return domain

    except Exception as e:
        logger.debug(f"CH API website lookup failed for {company_number}: {e}")

    return None


def find_domain_dns(company_name: str) -> List[str]:
    """
    Try to find company domain via DNS brute-force with multiple variations.

    Tries multiple name variations against multiple domain patterns.
    Returns ALL domains that resolve (for verification step to filter).
    """
    variations = get_name_variations(company_name)

    if not variations:
        return []

    found_domains = []

    # Try each variation against each pattern
    # Prioritise .co.uk and .com first
    priority_patterns = ["{name}.co.uk", "{name}.com"]
    other_patterns = [p for p in DOMAIN_PATTERNS if p not in priority_patterns]

    # First pass: try all variations with .co.uk and .com
    for pattern in priority_patterns:
        for slug in variations:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                found_domains.append(domain)
                if len(found_domains) >= 5:  # Cap at 5 candidates
                    return found_domains

    # Second pass: try first two variations with all other patterns
    for slug in variations[:2]:
        for pattern in other_patterns:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                found_domains.append(domain)
                if len(found_domains) >= 5:
                    return found_domains

    return found_domains


# =============================================================================
# DOMAIN VERIFICATION - NEW STEP
# =============================================================================

def fetch_page_text(url: str, timeout: float = 10.0) -> Optional[str]:
    """Fetch webpage and extract text content."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()

        # Get text, limit to first 50KB
        content = response.text[:50000].lower()

        # Strip HTML tags (simple approach)
        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
        content = re.sub(r'<[^>]+>', ' ', content)
        content = re.sub(r'\s+', ' ', content)

        return content

    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return None


def extract_postcode_outward(address) -> Optional[str]:
    """Extract outward postcode (first part) from address."""
    if not address or not isinstance(address, str):
        return None

    # UK postcode pattern - capture outward code
    match = re.search(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?)\s*[0-9][A-Z]{2}\b', address.upper())
    if match:
        return match.group(1)
    return None


def verify_domain_matches_company(
    domain: str,
    company_name: str,
    registered_address,
) -> dict:
    """
    Fetch domain homepage and verify it matches the company.

    Checks:
    1. Company name (or significant words) appear on page
    2. Postcode/address appears on page
    3. Page doesn't look like a parked domain or directory

    Returns:
        {
            "verified": True/False,
            "confidence": "high" | "medium" | "low" | "none",
            "reason": "...",
            "checks_passed": ["name", "postcode", ...]
        }
    """
    result = {
        "verified": False,
        "confidence": "none",
        "reason": "",
        "checks_passed": [],
    }

    # Handle NaN/None address
    if not isinstance(registered_address, str):
        registered_address = ""

    # Fetch homepage
    url = f"https://{domain}"
    page_text = fetch_page_text(url)

    if not page_text:
        # Fallback to HTTP if HTTPS fails (some older sites don't support HTTPS)
        url = f"http://{domain}"  # noqa: S310 - intentional HTTP fallback
        page_text = fetch_page_text(url)

    if not page_text:
        result["reason"] = "could_not_fetch"
        return result

    # Check for parked domain indicators
    parked_indicators = [
        "domain is for sale", "buy this domain", "domain parking",
        "this domain has expired", "domain available", "parked free",
        "godaddy", "namecheap", "domain registrar",
    ]
    for indicator in parked_indicators:
        if indicator in page_text:
            result["reason"] = "parked_domain"
            return result

    # =================================================================
    # Check 1: Company name words appear on page
    # =================================================================
    clean_name = clean_name_for_domain(company_name).replace('-', ' ')
    name_words = [w for w in clean_name.split() if len(w) >= 3]

    name_matches = 0
    for word in name_words:
        if word in page_text:
            name_matches += 1

    # Need at least 1 significant word, or 50%+ of words for longer names
    name_threshold = max(1, len(name_words) // 2)
    name_check_passed = name_matches >= name_threshold

    if name_check_passed:
        result["checks_passed"].append("name")

    # =================================================================
    # Check 2: Postcode appears on page
    # =================================================================
    outward_code = extract_postcode_outward(registered_address)
    postcode_check_passed = False

    if outward_code:
        # Check for outward code (e.g., "M34" from "M34 2SY")
        if outward_code.lower() in page_text:
            postcode_check_passed = True
            result["checks_passed"].append("postcode")

    # =================================================================
    # Check 3: Address keywords (city/town name)
    # =================================================================
    address_check_passed = False
    if registered_address:
        # Extract potential city/town names (words > 4 chars that aren't common)
        addr_words = re.findall(r'\b[A-Za-z]{5,}\b', registered_address.lower())
        skip_words = {'limited', 'street', 'house', 'building', 'floor', 'suite', 'office', 'united', 'kingdom'}
        addr_words = [w for w in addr_words if w not in skip_words]

        for word in addr_words:
            if word in page_text:
                address_check_passed = True
                result["checks_passed"].append("address")
                break

    # =================================================================
    # Determine confidence level
    # =================================================================
    checks_passed = len(result["checks_passed"])

    if checks_passed >= 2:
        result["verified"] = True
        result["confidence"] = "high"
        result["reason"] = f"passed {checks_passed} checks: {', '.join(result['checks_passed'])}"
    elif name_check_passed:
        result["verified"] = True
        result["confidence"] = "medium"
        result["reason"] = "name match only"
    elif postcode_check_passed or address_check_passed:
        result["verified"] = True
        result["confidence"] = "low"
        result["reason"] = "address match only (name not found)"
    else:
        result["verified"] = False
        result["confidence"] = "none"
        result["reason"] = "no matches found"

    return result


def find_and_verify_domain(
    company_name: str,
    registered_address,
    ch_domain: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Find domain via DNS and verify it matches the company.

    Returns: (domain, domain_source, verification_status)

    verification_status: "verified_high", "verified_medium", "verified_low", "unverified", "not_found"
    """

    # If we have a domain from CH API, trust it (most reliable source)
    if ch_domain:
        return ch_domain, "ch_api", "verified_high"

    # Find candidate domains via DNS
    candidate_domains = find_domain_dns(company_name)

    if not candidate_domains:
        return None, None, "not_found"

    # Verify each candidate, return first that passes
    for domain in candidate_domains:
        verification = verify_domain_matches_company(
            domain=domain,
            company_name=company_name,
            registered_address=registered_address,
        )

        if verification["verified"]:
            confidence = verification["confidence"]
            logger.debug(f"  ✓ Domain {domain} verified ({confidence}): {verification['reason']}")
            return domain, "dns_verified", f"verified_{confidence}"
        else:
            logger.debug(f"  ✗ Domain {domain} rejected: {verification['reason']}")

    # No domain passed verification - return first one as unverified
    # (User can decide whether to use unverified domains)
    return candidate_domains[0], "dns_unverified", "unverified"


# =============================================================================
# GOOGLE CUSTOM SEARCH FALLBACK
# =============================================================================

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# Domains to reject (directories, not company sites)
REJECTED_DOMAINS = {
    'companieshouse.gov.uk', 'find-and-update.company-information.service.gov.uk',
    'linkedin.com', 'facebook.com', 'twitter.com', 'instagram.com',
    'youtube.com', 'tiktok.com', 'pinterest.com',
    'wikipedia.org', 'bloomberg.com', 'reuters.com',
    'dnb.com', 'crunchbase.com', 'glassdoor.com', 'indeed.com',
    'yell.com', 'yelp.com', 'tripadvisor.com', 'trustpilot.com',
    'google.com', 'google.co.uk', 'bing.com',
    'endole.co.uk', 'duedil.com', 'companycheck.co.uk',
    'zoominfo.com', 'apollo.io', 'rocketreach.co', 'lusha.com',
}


def find_domain_google(company_name: str) -> Optional[str]:
    """Search Google for company website."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return None

    clean_name = clean_name_for_domain(company_name).replace('-', ' ')
    query = f"{clean_name} uk company website"

    try:
        response = requests.get(
            GOOGLE_SEARCH_URL,
            params={"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 5},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()

        for item in data.get("items", []):
            url = item.get("link", "")
            domain = urlparse(url).netloc.lower()
            if domain.startswith('www.'):
                domain = domain[4:]

            if domain and domain not in REJECTED_DOMAINS:
                return domain

        return None

    except Exception as e:
        logger.debug(f"Google search failed for {company_name}: {e}")
        return None


# =============================================================================
# DIRECTOR EXTRACTION
# =============================================================================

NAME_PREFIXES = {"mr", "mrs", "ms", "miss", "dr", "prof", "sir", "dame", "lord", "lady"}
NAME_SUFFIXES = {"obe", "mbe", "cbe", "dbe", "kbe", "fca", "aca", "fcca", "acca", "fcma"}

# Roles that count as "director" (excludes secretaries)
DIRECTOR_ROLES = {
    "director",
    "managing-director",
    "chief-executive-officer",
    "corporate-director",
}

# Roles to explicitly exclude
EXCLUDED_ROLES = {
    "secretary",
    "corporate-secretary",
    "nominee-secretary",
}


def parse_director_name(name: str) -> Tuple[str, str]:
    """Parse CH director name into (first, last)."""
    if not name:
        return "", ""

    name = name.strip()

    # Remove prefixes/suffixes
    words = name.split()
    filtered = [w for w in words if w.strip('.,').lower() not in NAME_PREFIXES | NAME_SUFFIXES]
    name = ' '.join(filtered)

    # Handle "LASTNAME, Firstname" format
    if ',' in name:
        parts = name.split(',', 1)
        last_name = parts[0].strip()
        first_part = parts[1].strip() if len(parts) > 1 else ""
        first_words = first_part.split()
        first_name = first_words[0] if first_words else ""
        return first_name.title(), last_name.title()

    # Handle "Firstname LASTNAME" format
    words = name.split()
    if len(words) >= 2:
        for i, word in enumerate(words):
            if word.isupper() and len(word) > 1:
                last_name = word
                first_name = words[0] if i > 0 else (words[1] if len(words) > 1 else "")
                return first_name.title(), last_name.title()
        return words[0].title(), words[-1].title()

    return "", name.title()


def get_best_director(client: CachedCompaniesHouseClient, company_number: str) -> Optional[dict]:
    """
    Get the longest-serving active director for contact purposes.

    Logic:
    1. Filter to active officers only (no resigned_on date)
    2. Exclude secretaries and other non-director roles
    3. Sort by appointment date (earliest = longest serving = most senior)
    4. Return the longest-serving director
    """
    try:
        officers = client.get_officers(company_number)

        # Filter to active directors only (exclude secretaries!)
        directors = []
        for o in officers:
            # Skip if resigned
            if o.get("resigned_on"):
                continue

            # Normalise role for comparison
            role = o.get("officer_role", "").lower().replace(" ", "-")

            # Skip excluded roles (secretaries etc)
            if role in EXCLUDED_ROLES:
                continue

            # Only include if it's a director role
            if role in DIRECTOR_ROLES or "director" in role:
                directors.append(o)

        if not directors:
            return None

        # Sort by appointment date (oldest first = longest serving)
        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except (ValueError, TypeError):
                return datetime.max

        directors.sort(key=lambda d: parse_date(d.get("appointed_on", "")))

        # Take the longest-serving director
        best_director = directors[0]
        first, last = parse_director_name(best_director.get("name", ""))

        return {
            "name": best_director.get("name", ""),
            "first": first,
            "last": last,
            "role": best_director.get("officer_role", ""),
            "appointed": best_director.get("appointed_on", ""),
        }

    except Exception as e:
        logger.debug(f"Failed to get officers for {company_number}: {e}")
        return None


# =============================================================================
# EMAIL VARIANT GENERATION
# =============================================================================

def generate_email_variants(first: str, last: str, domain: str) -> List[str]:
    """Generate email patterns to verify."""
    if not first or not last or not domain:
        return []

    first = first.lower().strip()
    last = last.lower().strip()

    # Remove hyphens/apostrophes from names for email
    first_clean = re.sub(r"['-]", "", first)
    last_clean = re.sub(r"['-]", "", last)

    fi = first_clean[0] if first_clean else ""

    return [
        f"{first_clean}@{domain}",
        f"{first_clean}.{last_clean}@{domain}",
        f"{fi}{last_clean}@{domain}",
        f"{fi}.{last_clean}@{domain}",
    ]


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _assign_result_columns(
    df: pd.DataFrame,
    domains: List,
    domain_sources: List,
    domain_verifications: List,
    director_names: List,
    director_firsts: List,
    director_lasts: List,
    director_roles: List,
    email_variants_list: List,
) -> None:
    """Assign enrichment result columns to dataframe."""
    df["domain"] = domains
    df["domain_source"] = domain_sources
    df["domain_verification"] = domain_verifications
    df["director_name"] = director_names
    df["director_first"] = director_firsts
    df["director_last"] = director_lasts
    df["director_role"] = director_roles
    df["email_variants"] = email_variants_list


# =============================================================================
# MAIN ENRICHMENT FUNCTION
# =============================================================================

def enrich_contacts(
    input_path: Path,
    output_path: Path,
    use_google_fallback: bool = False,
    limit: Optional[int] = None,
    checkpoint_every: int = 10,
) -> int:
    """
    Enrich leads with domain and director information.

    Domain finding strategy (in order):
    1. Check Companies House API for website on file (most reliable, auto-verified)
    2. DNS brute-force with multiple name variations + VERIFICATION
    3. Google Custom Search fallback (if enabled) + VERIFICATION

    Args:
        input_path: Path to tier_a_plus_leads.csv
        output_path: Path for output CSV
        use_google_fallback: Use Google search for missing domains
        limit: Limit number of leads to process (for testing)
        checkpoint_every: Save progress every N rows (default 10)

    Returns:
        Number of leads enriched
    """
    logger.info(f"Loading leads from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    if limit:
        df = df.head(limit)

    total = len(df)

    # =========================================================================
    # CHECKPOINT/RESUME LOGIC
    # =========================================================================
    checkpoint_path = output_path.parent / f".{output_path.stem}_checkpoint.csv"
    start_index = 0

    # Check for existing checkpoint
    if checkpoint_path.exists():
        try:
            checkpoint_df = pd.read_csv(checkpoint_path, dtype={"company_number": str})
            start_index = len(checkpoint_df)
            if 0 < start_index < total:
                logger.info(f"RESUMING from checkpoint: {start_index}/{total} already processed")
                logger.info(f"Checkpoint file: {checkpoint_path}")
            elif start_index >= total:
                logger.info(f"Checkpoint shows all {total} rows complete. Moving to final output.")
                checkpoint_df.to_csv(output_path, index=False)
                checkpoint_path.unlink()  # Remove checkpoint
                return total
        except Exception as e:
            logger.warning(f"Could not read checkpoint file, starting fresh: {e}")
            start_index = 0

    logger.info(f"Processing {total:,} leads (starting from row {start_index})")
    logger.info(f"Domain strategy: CH API → DNS + verify → {'Google fallback' if use_google_fallback else 'stop'}")
    logger.info(f"Checkpoint every {checkpoint_every} rows to {checkpoint_path}")

    # Initialize Companies House client for director lookup
    ch_client = CachedCompaniesHouseClient()

    # Load existing results from checkpoint or initialize empty lists
    if start_index > 0 and checkpoint_path.exists():
        checkpoint_df = pd.read_csv(checkpoint_path, dtype={"company_number": str})
        domains = checkpoint_df["domain"].tolist()
        domain_sources = checkpoint_df["domain_source"].tolist()
        domain_verifications = checkpoint_df["domain_verification"].tolist()
        director_names = checkpoint_df["director_name"].tolist()
        director_firsts = checkpoint_df["director_first"].tolist()
        director_lasts = checkpoint_df["director_last"].tolist()
        director_roles = checkpoint_df["director_role"].tolist()
        email_variants_list = checkpoint_df["email_variants"].tolist()
    else:
        domains = []
        domain_sources = []
        domain_verifications = []
        director_names = []
        director_firsts = []
        director_lasts = []
        director_roles = []
        email_variants_list = []

    # Stats (approximate - doesn't reload from checkpoint)
    ch_api_found = 0
    dns_verified_high = 0
    dns_verified_medium = 0
    dns_verified_low = 0
    dns_unverified = 0
    google_found = 0
    directors_found = 0

    for i, (_, row) in enumerate(df.iterrows()):
        # Skip already processed rows
        if i < start_index:
            continue

        company_number = str(row["company_number"])
        company_name = row.get("company_name", "")
        registered_address = row.get("registered_address", "")

        if (i + 1) % 10 == 0 or (i + 1) == total:
            logger.info(f"Progress: {i+1}/{total} ({100*(i+1)/total:.0f}%)")

        # =================================================================
        # Step 1: Check CH API for website (most reliable)
        # =================================================================
        ch_domain = get_domain_from_ch_api(ch_client, company_number)

        # =================================================================
        # Step 2: Find and verify domain
        # =================================================================
        domain, domain_source, verification_status = find_and_verify_domain(
            company_name=company_name,
            registered_address=registered_address,
            ch_domain=ch_domain,
        )

        # Track stats
        if domain_source == "ch_api":
            ch_api_found += 1
        elif domain_source == "dns_verified":
            if verification_status == "verified_high":
                dns_verified_high += 1
            elif verification_status == "verified_medium":
                dns_verified_medium += 1
            else:
                dns_verified_low += 1
        elif domain_source == "dns_unverified":
            dns_unverified += 1
            logger.warning(f"  ⚠ {company_name}: domain {domain} unverified")

        # Google fallback if no domain found
        if not domain and use_google_fallback:
            google_domain = find_domain_google(company_name)
            if google_domain:
                # Verify Google result too
                verification = verify_domain_matches_company(
                    domain=google_domain,
                    company_name=company_name,
                    registered_address=registered_address,
                )
                if verification["verified"]:
                    domain = google_domain
                    domain_source = "google_verified"
                    verification_status = f"verified_{verification['confidence']}"
                    google_found += 1
                time.sleep(0.5)  # Rate limit Google

        domains.append(domain)
        domain_sources.append(domain_source)
        domain_verifications.append(verification_status)

        # =================================================================
        # Step 3: Get longest-serving director
        # =================================================================
        director = get_best_director(ch_client, company_number)

        if director:
            directors_found += 1
            director_names.append(director["name"])
            director_firsts.append(director["first"])
            director_lasts.append(director["last"])
            director_roles.append(director["role"])

            # Step 4: Generate email variants
            variants = generate_email_variants(director["first"], director["last"], domain or "")
            email_variants_list.append("; ".join(variants) if variants else "")
        else:
            director_names.append("")
            director_firsts.append("")
            director_lasts.append("")
            director_roles.append("")
            email_variants_list.append("")

        # =================================================================
        # CHECKPOINT: Save progress every N rows
        # =================================================================
        if (i + 1) % checkpoint_every == 0 or (i + 1) == total:
            checkpoint_df = df.iloc[:i+1].copy()
            _assign_result_columns(checkpoint_df, domains, domain_sources, domain_verifications,
                                   director_names, director_firsts, director_lasts,
                                   director_roles, email_variants_list)

            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_df.to_csv(checkpoint_path, index=False)
            logger.debug(f"Checkpoint saved at row {i+1}")

    # Add columns to dataframe
    _assign_result_columns(df, domains, domain_sources, domain_verifications,
                           director_names, director_firsts, director_lasts,
                           director_roles, email_variants_list)

    # Write final output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    # Remove checkpoint file on successful completion
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        logger.info(f"Checkpoint file removed (complete)")

    # Log summary
    logger.info("=" * 50)
    logger.info("Contact enrichment complete")
    logger.info(f"  Total leads: {total:,}")
    logger.info("-" * 30)
    logger.info("Domain finding results:")
    logger.info(f"  From CH API (trusted):     {ch_api_found:,}")
    logger.info(f"  DNS verified (high):       {dns_verified_high:,}")
    logger.info(f"  DNS verified (medium):     {dns_verified_medium:,}")
    logger.info(f"  DNS verified (low):        {dns_verified_low:,}")
    logger.info(f"  DNS UNVERIFIED:            {dns_unverified:,} ⚠")
    if use_google_fallback:
        logger.info(f"  From Google (verified):    {google_found:,}")

    total_verified = ch_api_found + dns_verified_high + dns_verified_medium + dns_verified_low + google_found
    total_domains = total_verified + dns_unverified
    missing = total - total_domains

    logger.info("-" * 30)
    logger.info(f"  VERIFIED DOMAINS:          {total_verified:,} ({100*total_verified/total:.0f}%)")
    logger.info(f"  Unverified (risky):        {dns_unverified:,} ({100*dns_unverified/total:.0f}%)")
    logger.info(f"  Missing:                   {missing:,} ({100*missing/total:.0f}%)")
    logger.info("-" * 30)
    logger.info(f"Directors found:             {directors_found:,} ({100*directors_found/total:.0f}%)")
    logger.info(f"CH API requests:             {ch_client.request_count:,}")
    logger.info(f"Output: {output_path}")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich leads with domain and director contact information"
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Path to tier_a_plus_leads.csv",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path for output CSV",
    )
    parser.add_argument(
        "--google-fallback",
        action="store_true",
        help="Use Google Custom Search for missing domains (requires GOOGLE_API_KEY and GOOGLE_CX env vars)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Limit number of leads to process (for testing)",
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        logger.error(f"Input file not found: {input_path}")
        return

    if args.google_fallback and (not GOOGLE_API_KEY or not GOOGLE_CX):
        logger.warning("Google fallback requested but GOOGLE_API_KEY or GOOGLE_CX not set")
        logger.warning("Set environment variables: GOOGLE_API_KEY and GOOGLE_CX")

    enrich_contacts(
        input_path,
        output_path,
        use_google_fallback=args.google_fallback,
        limit=args.limit,
    )


if __name__ == "__main__":
    main()