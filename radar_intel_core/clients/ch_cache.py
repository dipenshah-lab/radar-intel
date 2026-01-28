"""
Companies House API client with SQLite caching.

Drop-in replacement for CompaniesHouseClient that caches API responses
to reduce quota usage when reprocessing data.

Usage:
    # Instead of:
    from radar_intel_core.clients.ch_client import CompaniesHouseClient
    client = CompaniesHouseClient()

    # Use:
    from radar_intel_core.clients.ch_cache import CachedCompaniesHouseClient
    client = CachedCompaniesHouseClient(cache_path="data/ch_cache.db")

    # All existing methods work identically
    profile = client.get_company_profile("12345678")
    pscs = client.get_pscs("12345678")
    chain = client.trace_ultimate_parent("12345678")

Cache TTL:
    - Company profiles: 30 days (changes rarely)
    - PSC data: 7 days (ownership can change)
    - Officers: 7 days

Drop this file into: radar_intel_core/clients/ch_cache.py
"""

from __future__ import annotations

import sqlite3
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import contextmanager

from radar_intel_core.clients.ch_client import CompaniesHouseClient

logger = logging.getLogger(__name__)

# Cache TTL settings (in days)
CACHE_TTL = {
    "company_profile": 30,  # Company details change rarely
    "pscs": 7,              # Ownership can change
    "officers": 7,          # Officers can change
    "default": 7,
}


class CHCache:
    """
    SQLite cache for Companies House API responses.

    Schema:
        - endpoint: The API endpoint called (e.g., "/company/12345678")
        - response_json: The JSON response as a string
        - cached_at: When the response was cached
        - expires_at: When the cache entry expires
    """

    def __init__(self, db_path: str = "data/ch_cache.db"):
        """
        Initialize the cache.

        Args:
            db_path: Path to SQLite database file. Created if it doesn't exist.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create the cache table if it doesn't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_cache (
                    endpoint TEXT PRIMARY KEY,
                    response_json TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires_at 
                ON api_cache(expires_at)
            """)
            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _get_ttl_days(endpoint: str) -> int:
        """Determine TTL based on endpoint type."""
        if "/persons-with-significant-control" in endpoint:
            return CACHE_TTL["pscs"]
        elif "/officers" in endpoint:
            return CACHE_TTL["officers"]
        elif endpoint.startswith("/company/") and endpoint.count("/") == 2:
            # /company/{number} - just the profile
            return CACHE_TTL["company_profile"]
        return CACHE_TTL["default"]

    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a cached response if it exists and hasn't expired.

        Args:
            endpoint: The API endpoint (e.g., "/company/12345678")

        Returns:
            The cached response dict, or None if not cached/expired
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT response_json FROM api_cache 
                WHERE endpoint = ? AND expires_at > ?
                """,
                (endpoint, now)
            )
            row = cursor.fetchone()

        if row:
            return json.loads(row[0])
        return None

    def set(self, endpoint: str, response: Dict[str, Any]) -> None:
        """
        Cache an API response.

        Args:
            endpoint: The API endpoint
            response: The response dict to cache
        """
        now = datetime.now(timezone.utc)
        ttl_days = self._get_ttl_days(endpoint)
        expires_at = now + timedelta(days=ttl_days)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO api_cache 
                (endpoint, response_json, cached_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    endpoint,
                    json.dumps(response),
                    now.isoformat(),
                    expires_at.isoformat(),
                )
            )
            conn.commit()

    def invalidate(self, endpoint: str) -> None:
        """Remove a specific cache entry."""
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM api_cache WHERE endpoint = ?",
                (endpoint,)
            )
            conn.commit()

    def cleanup_expired(self) -> int:
        """
        Remove all expired cache entries.

        Returns:
            Number of entries removed
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM api_cache WHERE expires_at <= ?",
                (now,)
            )
            count = cursor.rowcount
            conn.commit()

        if count > 0:
            logger.info(f"Cleaned up {count} expired cache entries")
        return count

    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dict with total_entries, expired_entries, size_bytes
        """
        now = datetime.now(timezone.utc).isoformat()

        with self._get_connection() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM api_cache"
            ).fetchone()[0]

            expired = conn.execute(
                "SELECT COUNT(*) FROM api_cache WHERE expires_at <= ?",
                (now,)
            ).fetchone()[0]

            valid = total - expired

        size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0

        return {
            "total_entries": total,
            "valid_entries": valid,
            "expired_entries": expired,
            "size_bytes": size_bytes,
            "size_mb": round(size_bytes / (1024 * 1024), 2),
        }

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM api_cache")
            conn.commit()
        logger.info("Cache cleared")


class CachedCompaniesHouseClient(CompaniesHouseClient):
    """
    Companies House API client with SQLite caching.

    Extends CompaniesHouseClient to cache API responses, dramatically
    reducing quota usage when reprocessing data or running the pipeline
    multiple times.

    The caching is transparent - all methods work identically to the
    base class, but responses are cached and reused when possible.

    Usage:
        client = CachedCompaniesHouseClient(cache_path="data/ch_cache.db")

        # First call hits the API
        profile = client.get_company_profile("12345678")

        # Second call returns cached response (no API call)
        profile = client.get_company_profile("12345678")

        # Check cache statistics
        client.print_stats()
    """

    def __init__(
            self,
            api_key: str | None = None,
            base_url: str | None = None,
            timeout: int = 10,
            backoff_seconds: float = 0.5,
            max_retries: int = 3,
            cache_path: str = "data/ch_cache.db",
            bypass_cache: bool = False,
    ) -> None:
        """
        Initialize the cached client.

        Args:
            api_key: Companies House API key (from config if not provided)
            base_url: API base URL (from config if not provided)
            timeout: Request timeout in seconds
            backoff_seconds: Initial backoff for retries
            max_retries: Maximum retry attempts
            cache_path: Path to SQLite cache database
            bypass_cache: If True, always hit the API (useful for testing)
        """
        # Initialize the base client
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            backoff_seconds=backoff_seconds,
            max_retries=max_retries,
        )

        self._cache = CHCache(cache_path)
        self._bypass_cache = bypass_cache

        # Cache statistics
        self._cache_hits = 0
        self._cache_misses = 0

        # Clean up expired entries on startup
        self._cache.cleanup_expired()

    def _get(
            self,
            endpoint: str,
            params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Internal GET with caching layer.

        Checks cache first, falls back to API, caches the response.
        """
        # Skip cache for parameterised queries (pagination etc)
        if params or self._bypass_cache:
            return super()._get(endpoint, params)

        # Check cache
        cached = self._cache.get(endpoint)
        if cached is not None:
            self._cache_hits += 1
            return cached

        # Cache miss - call the API
        self._cache_misses += 1
        response = super()._get(endpoint, params)

        # Cache the response (even empty {} for 404s)
        # This prevents repeated lookups for non-existent companies
        self._cache.set(endpoint, response)

        return response

    @property
    def cache_hits(self) -> int:
        """Number of cache hits this session."""
        return self._cache_hits

    @property
    def cache_misses(self) -> int:
        """Number of cache misses (API calls) this session."""
        return self._cache_misses

    @property
    def hit_rate(self) -> float:
        """Cache hit rate as a percentage."""
        total = self._cache_hits + self._cache_misses
        if total == 0:
            return 0.0
        return (self._cache_hits / total) * 100

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics."""
        cache_stats = self._cache.get_stats()
        return {
            **cache_stats,
            "session_cache_hits": self._cache_hits,
            "session_cache_misses": self._cache_misses,
            "session_api_calls": self._request_count,
            "session_hit_rate": f"{self.hit_rate:.1f}%",
        }

    def print_stats(self) -> None:
        """Print cache statistics to console."""
        cache_stats = self.get_cache_stats()
        print(f"\n{'=' * 50}")
        print("Companies House API Cache Statistics")
        print(f"{'=' * 50}")
        print(f"Cache file: {self._cache.db_path}")
        print(f"Cache size: {cache_stats['size_mb']} MB")
        print(f"Total entries: {cache_stats['total_entries']}")
        print(f"Valid entries: {cache_stats['valid_entries']}")
        print(f"Expired entries: {cache_stats['expired_entries']}")
        print(f"\nThis session:")
        print(f"  Cache hits: {cache_stats['session_cache_hits']}")
        print(f"  Cache misses: {cache_stats['session_cache_misses']}")
        print(f"  API calls: {cache_stats['session_api_calls']}")
        print(f"  Hit rate: {cache_stats['session_hit_rate']}")
        print(f"{'=' * 50}\n")

    def invalidate_company(self, company_number: str) -> None:
        """
        Invalidate all cached data for a specific company.

        Useful if you know data has changed and want fresh results.

        Args:
            company_number: Company number to invalidate
        """
        endpoints = [
            f"/company/{company_number}",
            f"/company/{company_number}/persons-with-significant-control",
            f"/company/{company_number}/officers",
        ]
        for endpoint in endpoints:
            self._cache.invalidate(endpoint)
        logger.info(f"Invalidated cache for company {company_number}")


# CLI for cache management
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Manage Companies House API cache")
    parser.add_argument(
        "--cache-path",
        default="data/ch_cache.db",
        help="Path to cache database",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Show cache statistics",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Remove expired entries",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Clear all cache entries",
    )

    args = parser.parse_args()

    cache = CHCache(args.cache_path)

    if args.stats:
        cache_info = cache.get_stats()
        print(f"\nCache: {args.cache_path}")
        print(f"Size: {cache_info['size_mb']} MB")
        print(f"Total entries: {cache_info['total_entries']}")
        print(f"Valid entries: {cache_info['valid_entries']}")
        print(f"Expired entries: {cache_info['expired_entries']}")

    if args.cleanup:
        removed = cache.cleanup_expired()
        print(f"Removed {removed} expired entries")

    if args.clear:
        confirm = input("Are you sure you want to clear all cache entries? (yes/no): ")
        if confirm.lower() == "yes":
            cache.clear()
            print("Cache cleared")
        else:
            print("Cancelled")