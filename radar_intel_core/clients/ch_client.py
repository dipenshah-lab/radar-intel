from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

from radar_intel_core.config import CH_API_KEY, CH_BASE_URL


class CompaniesHouseClient:
    """
    Minimal Companies House client for ESOS Radar use-cases.
    Wraps the public data API with a simple GET helper and a
    dedicated company profile method. [web:19][web:10]
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

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Internal GET with basic retry/backoff and API key auth.
        Returns parsed JSON (dict) or {} on hard failure. [web:19]
        """
        auth = (self.api_key, "")
        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    url,
                    params=params,
                    auth=auth,
                    timeout=self.timeout,
                )
                if resp.status_code == 200:
                    try:
                        return resp.json()
                    except ValueError:
                        return {}
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return {}
            except requests.RequestException:
                pass

            if attempt < self.max_retries - 1:
                time.sleep(self.backoff_seconds)

        return {}

    def get_company_profile(self, company_number: str) -> Dict[str, Any]:
        """
        Fetch a company profile from Companies House:
        GET /company/{company_number}. [web:21][web:10]
        """
        url = f"{self.base_url}/company/{company_number}"
        return self._get(url, params=None)
