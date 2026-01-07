"""
Companies House API client.

Provides methods for:
- Company profile lookup
- Persons with Significant Control (PSC) lookup (for parent tracing)

Rate limits: 600 requests per 5 minutes (enforced by CH)

References:
- API docs: https://developer.company-information.service.gov.uk/
- Company profile: GET /company/{company_number}
- PSC: GET /company/{company_number}/persons-with-significant-control
"""

from __future__ import annotations

import time
import logging
from typing import Any, Dict, List, Optional

import requests

from radar_intel_core.config import CH_API_KEY, CH_BASE_URL

logger = logging.getLogger(__name__)

# PSC kinds that indicate corporate ownership (not individuals)
CORPORATE_PSC_KINDS = {
    "corporate-entity-person-with-significant-control",
    "corporate-entity-beneficial-owner",
    "legal-person-person-with-significant-control",
    "legal-person-beneficial-owner",
    "super-secure-person-with-significant-control",  # Hidden but may be corporate
}


class CompaniesHouseClient:
    """
    Companies House API client for ESOS Radar.

    Handles authentication, rate limiting, retries, and provides
    methods for company profiles and PSC lookups.
    """

    def __init__(
            self,
            api_key: str | None = None,
            base_url: str | None = None,
            timeout: int = 10,
            backoff_seconds: float = 0.5,
            max_retries: int = 3,
    ) -> None:
        self.api_key = api_key or CH_API_KEY
        self.base_url = (base_url or CH_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.backoff_seconds = backoff_seconds
        self.max_retries = max_retries

        # Track request count for rate limit awareness
        self._request_count = 0

    def _get(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Internal GET request with retry/backoff and API key auth.

        Args:
            endpoint: API endpoint (e.g., "/company/12345678")
            params: Optional query parameters

        Returns:
            Parsed JSON response or {} on failure
        """
        url = f"{self.base_url}{endpoint}"
        auth = (self.api_key, "")

        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    auth=auth,
                    timeout=self.timeout,
                )
                self._request_count += 1

                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        return {}

                # 404 = company not found (valid response, not an error)
                if resp.status_code == 404:
                    return {}

                # 429 = rate limited, back off longer
                if resp.status_code == 429:
                    wait_time = self.backoff_seconds * (2 ** attempt) * 10
                    logger.warning(f"Rate limited, waiting {wait_time:.1f}s")
                    time.sleep(wait_time)
                    continue

                # Other 4xx = client error, don't retry
                if 400 <= resp.status_code < 500:
                    logger.debug(f"Client error {resp.status_code} for {endpoint}")
                    return {}

                # 5xx = server error, retry
                if resp.status_code >= 500:
                    logger.warning(f"Server error {resp.status_code}, retrying...")

            except requests.RequestException as e:
                logger.warning(f"Request failed: {e}")

            if attempt < self.max_retries - 1:
                time.sleep(self.backoff_seconds * (attempt + 1))

        return {}

    def get_company_profile(self, company_number: str) -> Dict[str, Any]:
        """
        Fetch company profile from Companies House.

        GET /company/{company_number}

        Args:
            company_number: 8-character company registration number

        Returns:
            Company profile dict or {} if not found
        """
        endpoint = f"/company/{company_number}"
        return self._get(endpoint)

    def get_pscs(self, company_number: str) -> List[Dict[str, Any]]:
        """
        Fetch Persons with Significant Control for a company.

        GET /company/{company_number}/persons-with-significant-control

        Args:
            company_number: 8-character company registration number

        Returns:
            List of PSC records (may be empty)
        """
        endpoint = f"/company/{company_number}/persons-with-significant-control"
        result = self._get(endpoint)

        # PSC endpoint returns {"items": [...], "total_results": N, ...}
        return result.get("items", [])

    def get_corporate_pscs(self, company_number: str) -> List[Dict[str, Any]]:
        """
        Fetch only corporate PSCs (not individuals) for a company.

        These indicate corporate ownership - useful for tracing parent companies.

        Args:
            company_number: 8-character company registration number

        Returns:
            List of corporate PSC records
        """
        all_pscs = self.get_pscs(company_number)

        return [
            psc for psc in all_pscs
            if psc.get("kind") in CORPORATE_PSC_KINDS
        ]

    def trace_ultimate_parent(
            self,
            company_number: str,
            max_depth: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Trace corporate ownership chain to find ultimate parent.

        Follows PSC chain upward until we hit:
        - A company with no corporate PSCs (top of chain)
        - A foreign entity (can't trace further via CH)
        - Max depth reached
        - Circular reference detected

        Args:
            company_number: Starting company number
            max_depth: Maximum levels to trace (prevents infinite loops)

        Returns:
            List of dicts representing the ownership chain, from child to parent.
            Each dict has: company_number, company_name, is_foreign, depth
        """
        chain = []
        visited = set()
        current = company_number

        for depth in range(max_depth):
            if current in visited:
                logger.warning(f"Circular reference detected at {current}")
                break

            visited.add(current)

            # Get company profile for name
            profile = self.get_company_profile(current)
            company_name = profile.get("company_name", "Unknown")

            chain.append({
                "company_number": current,
                "company_name": company_name,
                "is_foreign": False,
                "depth": depth,
            })

            # Get corporate PSCs
            corporate_pscs = self.get_corporate_pscs(current)

            if not corporate_pscs:
                # No corporate owner - this is the ultimate parent
                break

            # Take the first corporate PSC as the parent
            # (Companies can have multiple, but we follow the primary)
            parent_psc = corporate_pscs[0]
            identification = parent_psc.get("identification", {})

            # Check if foreign
            country = identification.get("country_registered", "")
            is_uk = country.lower() in [
                "united kingdom",
                "england",
                "wales",
                "scotland",
                "northern ireland",
                "england and wales",
                "",  # Empty often means UK
            ]

            if not is_uk:
                # Foreign parent - record it but can't trace further
                chain.append({
                    "company_number": None,
                    "company_name": parent_psc.get("name", "Unknown Foreign Entity"),
                    "is_foreign": True,
                    "depth": depth + 1,
                    "country": country,
                })
                break

            # Get parent company number
            parent_number = identification.get("registration_number")

            if not parent_number:
                # No registration number - can't trace further
                chain.append({
                    "company_number": None,
                    "company_name": parent_psc.get("name", "Unknown"),
                    "is_foreign": False,
                    "depth": depth + 1,
                    "note": "No registration number available",
                })
                break

            current = parent_number

        return chain

    def get_ultimate_parent_number(
            self,
            company_number: str,
            max_depth: int = 10,
    ) -> Optional[str]:
        """
        Get the company number of the ultimate UK parent, if traceable.

        Args:
            company_number: Starting company number
            max_depth: Maximum levels to trace

        Returns:
            Ultimate parent company number, or None if foreign/untraceable
        """
        chain = self.trace_ultimate_parent(company_number, max_depth)

        if not chain:
            return None

        # Find the last UK company in the chain
        for entity in reversed(chain):
            if entity.get("company_number") and not entity.get("is_foreign"):
                return entity["company_number"]

        return None

    @property
    def request_count(self) -> int:
        """Number of API requests made by this client instance."""
        return self._request_count
