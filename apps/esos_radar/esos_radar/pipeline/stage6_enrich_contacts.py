"""
Stage 6: Enrich leads with domain and director contact information.

Input:  tier_a_plus_leads.csv (from Stage 5)
Output: enriched_leads.csv (with domains, directors, email variants)

Three-step process:
1. Find company domains via DNS brute-force
2. Extract director names from Companies House API
3. Generate email variants for verification

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
import os

import pandas as pd
import requests

from radar_intel_core.clients.ch_client import CompaniesHouseClient


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

    from urllib.parse import urlparse

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


def get_domain_from_ch_api(client: CompaniesHouseClient, company_number: str) -> Optional[str]:
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


def find_domain_dns(company_name: str) -> Optional[str]:
    """
    Try to find company domain via DNS brute-force with multiple variations.

    Tries multiple name variations against multiple domain patterns.
    Returns first domain that resolves.
    """
    variations = get_name_variations(company_name)

    if not variations:
        return None

    # Try each variation against each pattern
    # Prioritise .co.uk and .com first
    priority_patterns = ["{name}.co.uk", "{name}.com"]
    other_patterns = [p for p in DOMAIN_PATTERNS if p not in priority_patterns]

    # First pass: try all variations with .co.uk and .com
    for pattern in priority_patterns:
        for slug in variations:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                return domain

    # Second pass: try first two variations with all other patterns
    for slug in variations[:2]:
        for pattern in other_patterns:
            domain = pattern.format(name=slug)
            if check_domain_exists(domain):
                return domain

    return None


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
            from urllib.parse import urlparse
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


def get_best_director(client: CompaniesHouseClient, company_number: str) -> Optional[dict]:
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
    fi = first[0] if first else ""

    return [
        f"{first}@{domain}",
        f"{first}.{last}@{domain}",
        f"{fi}{last}@{domain}",
        f"{fi}.{last}@{domain}",
    ]


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

    Domain finding strategy (in order):
    1. Check Companies House API for website on file (most reliable)
    2. DNS brute-force with multiple name variations
    3. Google Custom Search fallback (if enabled)

    Args:
        input_path: Path to tier_a_plus_leads.csv
        output_path: Path for output CSV
        use_google_fallback: Use Google search for missing domains
        limit: Limit number of leads to process (for testing)

    Returns:
        Number of leads enriched
    """
    logger.info(f"Loading leads from {input_path}")
    df = pd.read_csv(input_path, dtype={"company_number": str})

    if limit:
        df = df.head(limit)

    total = len(df)
    logger.info(f"Processing {total:,} leads")
    logger.info(f"Domain strategy: CH API → DNS variations → {'Google fallback' if use_google_fallback else 'stop'}")

    # Initialize Companies House client for director lookup
    ch_client = CompaniesHouseClient()

    # Results columns
    domains = []
    domain_sources = []
    director_names = []
    director_firsts = []
    director_lasts = []
    director_roles = []
    email_variants_list = []

    # Stats
    ch_api_found = 0
    dns_found = 0
    google_found = 0
    directors_found = 0

    for i, (_, row) in enumerate(df.iterrows()):
        company_number = str(row["company_number"])
        company_name = row.get("company_name", "")

        if (i + 1) % 20 == 0:
            logger.info(f"Progress: {i+1}/{total} ({100*(i+1)/total:.0f}%)")

        # =================================================================
        # Step 1: Find domain (multi-strategy)
        # =================================================================
        domain_source: Optional[str] = None

        # Strategy 1: Check CH API for website on file (most reliable)
        domain = get_domain_from_ch_api(ch_client, company_number)
        if domain:
            domain_source = "ch_api"
            ch_api_found += 1
        else:
            # Strategy 2: DNS brute-force with name variations
            domain = find_domain_dns(company_name)
            if domain:
                domain_source = "dns"
                dns_found += 1
            elif use_google_fallback:
                # Strategy 3: Google Custom Search fallback
                domain = find_domain_google(company_name)
                if domain:
                    domain_source = "google"
                    google_found += 1
                    time.sleep(0.5)  # Rate limit Google

        domains.append(domain)
        domain_sources.append(domain_source)

        # =================================================================
        # Step 2: Get longest-serving director
        # =================================================================
        director = get_best_director(ch_client, company_number)

        if director:
            directors_found += 1
            director_names.append(director["name"])
            director_firsts.append(director["first"])
            director_lasts.append(director["last"])
            director_roles.append(director["role"])

            # Step 3: Generate email variants
            variants = generate_email_variants(director["first"], director["last"], domain or "")
            email_variants_list.append("; ".join(variants) if variants else "")
        else:
            director_names.append("")
            director_firsts.append("")
            director_lasts.append("")
            director_roles.append("")
            email_variants_list.append("")

    # Add columns to dataframe
    df["domain"] = domains
    df["domain_source"] = domain_sources
    df["director_name"] = director_names
    df["director_first"] = director_firsts
    df["director_last"] = director_lasts
    df["director_role"] = director_roles
    df["email_variants"] = email_variants_list

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)

    # Log summary
    logger.info("=" * 50)
    logger.info("Contact enrichment complete")
    logger.info(f"  Total leads: {total:,}")
    logger.info("-" * 30)
    logger.info("Domain finding results:")
    logger.info(f"  From CH API:    {ch_api_found:,} ({100*ch_api_found/total:.0f}%)")
    logger.info(f"  From DNS:       {dns_found:,} ({100*dns_found/total:.0f}%)")
    if use_google_fallback:
        logger.info(f"  From Google:    {google_found:,} ({100*google_found/total:.0f}%)")
    total_domains = ch_api_found + dns_found + google_found
    logger.info(f"  TOTAL FOUND:    {total_domains:,} ({100*total_domains/total:.0f}%)")
    missing = total - total_domains
    logger.info(f"  Missing:        {missing:,} ({100*missing/total:.0f}%)")
    logger.info("-" * 30)
    logger.info(f"Directors found:  {directors_found:,} ({100*directors_found/total:.0f}%)")
    logger.info(f"CH API requests:  {ch_client.request_count:,}")
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