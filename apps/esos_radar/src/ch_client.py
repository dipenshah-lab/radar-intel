# src/ch_client.py
import time
from typing import Dict, Iterable, List, Optional
import requests

from .config import CH_API_KEY, CH_ADVANCED_SEARCH_URL, CH_PAGE_SIZE, CH_MAX_RESULTS


class CompaniesHouseClient:
    def __init__(self, api_key: str = CH_API_KEY, rate_limit_per_sec: float = 10.0):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.auth = (self.api_key, "")
        self.min_interval = 1.0 / rate_limit_per_sec

    def _get(self, url: str, params: Dict) -> Dict:
        start = time.time()
        resp = self.session.get(url, params=params, timeout=30)
        elapsed = time.time() - start
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        resp.raise_for_status()
        return resp.json()

    def advanced_search_page(
        self,
        start_index: int = 0,
        size: int = CH_PAGE_SIZE,
        *,
        company_types: Optional[Iterable[str]] = None,
        company_statuses: Optional[Iterable[str]] = None,
        location: Optional[str] = None,
        incorporated_from: Optional[str] = None,
        incorporated_to: Optional[str] = None,
        sic_codes: Optional[Iterable[str]] = None,
    ) -> Dict:
        """
        Single page call to /advanced-search/companies.[web:1]
        Dates should be 'YYYY-MM-DD' if supplied.
        """
        params: Dict[str, str] = {
            "start_index": str(start_index),
            "size": str(size),
        }

        if company_types:
            params["company_type"] = ",".join(company_types)
        if company_statuses:
            params["company_status"] = ",".join(company_statuses)
        if location:
            params["location"] = location
        if incorporated_from:
            params["incorporated_from"] = incorporated_from
        if incorporated_to:
            params["incorporated_to"] = incorporated_to
        if sic_codes:
            params["sic_codes"] = ",".join(sic_codes)

        return self._get(CH_ADVANCED_SEARCH_URL, params=params)

    def advanced_search_all(
        self,
        *,
        max_results: int = CH_MAX_RESULTS,
        **kwargs,
    ) -> List[Dict]:
        """
        Page through advanced search up to max_results.
        Returns raw 'items' list.
        """
        results: List[Dict] = []
        start_index = 0

        while True:
            page = self.advanced_search_page(start_index=start_index, **kwargs)
            items = page.get("items", [])
            results.extend(items)

            if len(results) >= max_results:
                return results[:max_results]

            returned = len(items)
            total_results = page.get("total_results", 0)
            if returned == 0 or start_index + returned >= total_results:
                break

            start_index += returned

        return results