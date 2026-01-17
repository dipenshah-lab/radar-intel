"""
Stage 6: Enrich leads with domain and director contact information.

Input:  tier_a_plus_leads.csv (from Stage 5)
Output: enriched_leads.csv (with domains, directors, email variants)

Four-step process:
1. Find company domains via DNS brute-force (or Google fallback)
2. VERIFY domain matches company (fetch homepage, check for name/address)
3. Extract director names from Companies House API
4. Generate email variants for verification, with quality flags

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
import os
import re
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict

from urllib.parse import urlparse

import pandas as pd
import requests

from radar_intel_core.clients.ch_client import CompaniesHouseClient  # adjust import if needed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# =============================================================================
# DOMAIN FINDING - MULTI-STRATEGY APPROACH
# =============================================================================

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

    for suffix in LEGAL_SUFFIXES:
        name = re.sub(rf'\b{suffix}\b\.?', '', name)

    for word in NOISE_WORDS:
        name = re.sub(rf'\b{word}\b', '', name)

    name = re.sub(r'[^\w\s]', '', name)
    name = '-'.join(name.split())

    return name.strip('-')


def get_name_variations(name: str) -> List[str]:
    """
    Generate multiple slug variations to maximise DNS hit rate.
    """
    base = clean_name_for_domain(name)
    if not base:
        return []

    words = base.split('-')
    no_hyphens = base.replace('-', '')

    variations = [base]

    if no_hyphens != base:
        variations.append(no_hyphens)

    if len(words) >= 1 and len(words[0]) >= 3:
        variations.append(words[0])

    if len(words) >= 2:
        first_two = '-'.join(words[:2])
        variations.append(first_two)
        variations.append(first_two.replace('-', ''))

    if len(words) >= 3:
        first_three = '-'.join(words[:3])
        variations.append(first_three)
        variations.append(first_three.replace('-', ''))

    if len(words) >= 2:
        initials = ''.join(w[0] for w in words if w)
        if len(initials) >= 2:
            variations.append(initials)

    if len(words) >= 2:
        first_plus_initials = words[0] + ''.join(w[0] for w in words[1:] if w)
        variations.append(first_plus_initials)

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

    schemes = ('http', 'https')
    has_scheme = any(url.startswith(f'{s}://') for s in schemes)
    if not has_scheme:
        url = 'https://' + url

    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if domain.startswith('www.'):
            domain = domain[4:]

        if ':' in domain:
            domain = domain.split(':')[0]

        return domain or None
    except (ValueError, AttributeError):
        return None


def get_domain_from_ch_api(client: CompaniesHouseClient, company_number: str) -> Optional[str]:
    """
    Check Companies House API for website/links on file.
    """
    try:
        profile = client.get_company(company_number)

        if profile.get("website"):
            domain = extract_domain_from_url(profile["website"])
            if domain:
                return domain

        links = profile.get("links", {})
        if links.get("website"):
            domain = extract_domain_from_url(links["website"])
            if domain:
                return domain

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
    Returns ALL domains that resolve (for verification step to filter).
    """
    variations = get_name_variations(company_name)
    if not variations:
        return []

    found_domains: List[str] = []

    priority_patterns = ["{name}.co.uk", "{name}.com"]
    other_patterns = [p for p in DOMAIN_PATTERNS if p not in priority_patterns]

    for pattern in priority_patterns:
        for slug in variations:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                found_domains.append(domain)
                if len(found_domains) >= 5:
                    return found_domains

    for slug in variations[:2]:
        for pattern in other_patterns:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                found_domains.append(domain)
                if len(found_domains) >= 5:
                    return found_domains

    return found_domains

# =============================================================================
# DOMAIN VERIFICATION
# =============================================================================

PARKED_INDICATORS = [
    "domain is for sale", "buy this domain", "domain parking",
    "this domain has expired", "domain available", "parked free",
    "godaddy", "namecheap", "domain registrar",
]

# Very generic sector-like words which should not on their own give high confidence
GENERIC_NAME_WORDS = {
    "services", "solutions", "group", "holdings", "international",
    "construction", "engineering", "consulting", "transport",
    "logistics", "care", "homes", "manufacturing",
}


def fetch_page_text(url: str, timeout: float = 10.0) -> Optional[str]:
    """Fetch webpage and extract text content."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }

    try:
        response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
        content = response.text[:50000].lower()

        content = re.sub(r'<script[^>]*>.*?</script>', '', content, flags=re.DOTALL)
        content = re.sub(r'<style[^>]*>.*?</style>', '', content, flags=re.DOTALL)
        content = re.sub(r'<[^>]+>', ' ', content)
        content = re.sub(r'\s+', ' ', content)

        return content
    except requests.exceptions.RequestException as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return None


def extract_postcode_outward(address: str) -> Optional[str]:
    """Extract outward postcode (first part) from address."""
    if not address:
        return None

    match = re.search(r'\b([A-Z]{1,2}[0-9][0-9A-Z]?)\s*[0-9][A-Z]{2}\b', address.upper())
    if match:
        return match.group(1)
    return None


def verify_domain_matches_company(
    domain: str,
    company_name: str,
    registered_address: str,
) -> Dict[str, object]:
    """
    Fetch domain homepage and verify it matches the company.

    Returns:
        {
            "verified": True/False,
            "confidence": "high" | "medium" | "low" | "none",
            "reason": "...",
            "checks_passed": ["name", "postcode", "address"]
        }
    """
    result: Dict[str, object] = {
        "verified": False,
        "confidence": "none",
        "reason": "",
        "checks_passed": [],
    }

    url = f"https://{domain}"
    page_text = fetch_page_text(url)

    if not page_text:
        url = f"http://{domain}"
        page_text = fetch_page_text(url)

    if not page_text:
        result["reason"] = "could_not_fetch"
        return result

    for indicator in PARKED_INDICATORS:
        if indicator in page_text:
            result["reason"] = "parked_domain"
            return result

    clean_name = clean_name_for_domain(company_name).replace('-', ' ')
    name_words = [w for w in clean_name.split() if len(w) >= 3]
    generic_only = all(w in GENERIC_NAME_WORDS for w in name_words) if name_words else False

    name_matches = sum(1 for w in name_words if w in page_text)
    name_threshold = max(1, len(name_words) // 2)
    name_check_passed = name_matches >= name_threshold

    if name_check_passed:
        result["checks_passed"].append("name")

    outward_code = extract_postcode_outward(registered_address)
    postcode_check_passed = False
    if outward_code and outward_code.lower() in page_text:
        postcode_check_passed = True
        result["checks_passed"].append("postcode")

    address_check_passed = False
    if registered_address:
        addr_words = re.findall(r'\b[A-Za-z]{5,}\b', registered_address.lower())
        skip_words = {
            'limited', 'street', 'house', 'building', 'floor',
            'suite', 'office', 'united', 'kingdom', 'road',
            'avenue', 'drive', 'lane',
        }
        addr_words = [w for w in addr_words if w not in skip_words]

        for word in addr_words:
            if word in page_text:
                address_check_passed = True
                result["checks_passed"].append("address")
                break

    checks_passed = len(result["checks_passed"])

    # Confidence rules:
    # - If company name is generic-ish, require name + (postcode or address) for "high"
    if checks_passed >= 2:
        if generic_only and "name" in result["checks_passed"] and not (
            "postcode" in result["checks_passed"] or "address" in result["checks_passed"]
        ):
            # downgrade: generic name without address evidence
            result["verified"] = True
            result["confidence"] = "medium"
            result["reason"] = f"generic name; passed checks: {', '.join(result['checks_passed'])}"
        else:
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
        result["reason"] = "address/postcode match only (name not found)"
    else:
        result["verified"] = False
        result["confidence"] = "none"
        result["reason"] = "no matches found"

    return result


def find_and_verify_domain(
    company_name: str,
    registered_address: str,
    ch_domain: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    Find domain via DNS and verify it matches the company.

    Returns: (domain, domain_source, verification_status)
    verification_status: "verified_high", "verified_medium", "verified_low", "unverified", "not_found"
    """
    if ch_domain:
        return ch_domain, "ch_api", "verified_high"

    candidate_domains = find_domain_dns(company_name)
    if not candidate_domains:
        return None, None, "not_found"

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

    return candidate_domains[0], "dns_unverified", "unverified"

# =============================================================================
# GOOGLE CUSTOM SEARCH FALLBACK
# =============================================================================

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CX = os.environ.get("GOOGLE_CX", "")
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

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
            timeout=10,
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

DIRECTOR_ROLES = {
    "director",
    "managing-director",
    "chief-executive-officer",
    "corporate-director",
}

EXCLUDED_ROLES = {
    "secretary",
    "corporate-secretary",
    "nominee-secretary",
}


def parse_director_name(name: str) -> Tuple[str, str, str]:
    """
    Parse CH director name into (first, last, complexity_flag).
    complexity_flag: "simple" | "multi_word" | "double_barrel" | "unknown"
    """
    if not name:
        return "", "", "unknown"

    raw = name.strip()
    words = raw.split()
    filtered = [w for w in words if w.strip('.,').lower() not in (NAME_PREFIXES | NAME_SUFFIXES)]
    name_clean = ' '.join(filtered)

    complexity = "simple"
    if '-' in name_clean:
        complexity = "double_barrel"
    elif len(name_clean.split()) > 2:
        complexity = "multi_word"

    # Handle "LASTNAME, Firstname"
    if ',' in name_clean:
        parts = name_clean.split(',', 1)
        last_name = parts[0].strip()
        first_part = parts[1].strip() if len(parts) > 1 else ""
        first_words = first_part.split()
        first_name = first_words[0] if first_words else ""
        return first_name.title(), last_name.title(), complexity

    # "Firstname LASTNAME"
    tokens = name_clean.split()
    if len(tokens) >= 2:
        for i, word in enumerate(tokens):
            if word.isupper() and len(word) > 1:
                last_name = word
                first_name = tokens[0] if i > 0 else (tokens[1] if len(tokens) > 1 else "")
                return first_name.title(), last_name.title(), complexity
        return tokens[0].title(), tokens[-1].title(), complexity

    return "", name_clean.title(), "unknown"


def get_best_director(client: CompaniesHouseClient, company_number: str) -> Optional[dict]:
    """
    Get the longest-serving active director for contact purposes.
    """
    try:
        officers = client.get_officers(company_number)
        directors = []

        for o in officers:
            if o.get("resigned_on"):
                continue

            role = o.get("officer_role", "").lower().replace(" ", "-")
            if role in EXCLUDED_ROLES:
                continue

            if role in DIRECTOR_ROLES or "director" in role:
                directors.append(o)

        if not directors:
            return None

        def parse_date(d):
            try:
                return datetime.strptime(d, "%Y-%m-%d")
            except (ValueError, TypeError):
                return datetime.max

        directors.sort(key=lambda d: parse_date(d.get("appointed_on", "")))
        best_director = directors[0]
        first, last, complexity = parse_director_name(best_director.get("name", ""))

        return {
            "name": best_director.get("name", ""),
            "first": first,
            "last": last,
            "role": best_director.get("officer_role", ""),
            "appointed": best_director.get("appointed_on", ""),
            "name_complexity": complexity,
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

    first_clean = re.sub(r"['-]", "", first)
    last_clean = re.sub(r"['-]", "", last)
    fi = first_clean[0] if first_clean else ""

    variants = [
        f"{first_clean}@{domain}",
        f"{first_clean}.{last_clean}@{domain}",
        f"{fi}{last_clean}@{domain}",
        f"{fi}.{last_clean}@{domain}",
    ]

    # Optional extra variant for double-barrelled surnames: use first part only
    if '-' in last:
        first_part = last.split('-')[0].lower()
        first_part_clean = re.sub(r"['-]", "", first_part)
        if first_part_clean and first_part_clean != last_clean:
            variants.append(f"{first_clean}.{first_part_clean}@{domain}")

    seen = set()
    unique = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            unique.append(v)

    return unique

# =============================================================================
# MAIN ENRICHMENT FUNCTION
# =============================================================================

def enrich_contacts(
    input_path: Path,
    output_path: Path,
    use_google_fallback: bool = False,
    limit: Optional[int] = None,
) -> int:
    """
    Enrich leads with domain and director information.
    """
    logger.info(f"Loading leads from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    if limit:
        df = df.head(limit)

    total = len(df)
    logger.info(f"Processing {total:,} leads")
    logger.info(f"Domain strategy: CH API → DNS + verify → {'Google fallback' if use_google_fallback else 'stop'}")

    ch_client = CompaniesHouseClient()

    domains: List[Optional[str]] = []
    domain_sources: List[Optional[str]] = []
    domain_verifications: List[str] = []
    use_for_email_flags: List[bool] = []

    director_names: List[str] = []
    director_firsts: List[str] = []
    director_lasts: List[str] = []
    director_roles: List[str] = []
    director_complexities: List[str] = []
    email_variants_list: List[str] = []

    ch_api_found = 0
    dns_verified_high = 0
    dns_verified_medium = 0
    dns_verified_low = 0
    dns_unverified = 0
    google_found = 0
    directors_found = 0

    for i, (_, row) in enumerate(df.iterrows()):
        company_number = str(row["company_number"])
        company_name = row.get("company_name", "")
        registered_address = row.get("registered_address", "")

        if (i + 1) % 10 == 0 or (i + 1) == total:
            logger.info(f"Progress: {i+1}/{total} ({100*(i+1)/total:.0f}%)")

        ch_domain = get_domain_from_ch_api(ch_client, company_number)

        domain, domain_source, verification_status = find_and_verify_domain(
            company_name=company_name,
            registered_address=registered_address,
            ch_domain=ch_domain,
        )

        if domain_source == "ch_api":
            ch_api_found += 1
        elif domain_source == "dns_verified":
            if verification_status == "verified_high":
                dns_verified_high += 1
            elif verification_status == "verified_medium":
                dns_verified_medium += 1
            elif verification_status == "verified_low":
                dns_verified_low += 1
        elif domain_source == "dns_unverified":
            dns_unverified += 1
            logger.warning(f"  ⚠ {company_name}: domain {domain} unverified")

        if not domain and use_google_fallback:
            google_domain = find_domain_google(company_name)
            if google_domain:
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
                else:
                    domain_source = "google_unverified"
                    verification_status = "unverified"
                time.sleep(0.5)

        domains.append(domain)
        domain_sources.append(domain_source)
        domain_verifications.append(verification_status)

        # Use domain_verification to decide if 6b should even try email
        use_for_email = verification_status in ("verified_high", "verified_medium")
        use_for_email_flags.append(bool(domain and use_for_email))

        director = get_best_director(ch_client, company_number)

        if director:
            directors_found += 1
            director_names.append(director["name"])
            director_firsts.append(director["first"])
            director_lasts.append(director["last"])
            director_roles.append(director["role"])
            director_complexities.append(director.get("name_complexity", "unknown"))

            variants = generate_email_variants(director["first"], director["last"], domain or "")
            email_variants_list.append("; ".join(variants) if variants else "")
        else:
            director_names.append("")
            director_firsts.append("")
            director_lasts.append("")
            director_roles.append("")
            director_complexities.append("unknown")
            email_variants_list.append("")

    df["domain"] = domains
    df["domain_source"] = domain_sources
    df["domain_verification"] = domain_verifications
    df["use_for_email"] = use_for_email_flags

    df["director_name"] = director_names
    df["director_first"] = director_firsts
    df["director_last"] = director_lasts
    df["director_role"] = director_roles
    df["director_name_complexity"] = director_complexities
    df["email_variants"] = email_variants_list

    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

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
