"""Tool result caching with per-tool TTL.

Caches expensive tool execution results in memory so repeated identical
calls within the TTL window are served instantly without re-execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default TTL in seconds when no per-tool override is set.
DEFAULT_TTL_SECONDS = 300  # 5 minutes

# Maximum cache entries before LRU eviction.
DEFAULT_MAX_ENTRIES = 256


@dataclass
class CacheEntry:
    """A single cached tool result."""

    tool_name: str
    result: str
    created_at: float
    ttl_seconds: float
    is_error: bool = False

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


def _cache_key(tool_name: str, tool_input: dict) -> str:
    """Generate a deterministic cache key from tool name and input.

    Sorts dict keys to ensure identical inputs produce the same key
    regardless of insertion order.
    """
    normalized = json.dumps({"tool": tool_name, "input": tool_input}, sort_keys=True)
    return hashlib.sha256(normalized.encode()).hexdigest()


class ToolResultCache:
    """In-memory cache for tool execution results with per-tool TTL.

    Usage::

        cache = ToolResultCache(
            tool_ttls={"check_weather": 1800, "read_email": 300},
            default_ttl=300,
        )

        # Check cache before executing
        hit = cache.get("check_weather", {"location": "NYC"})
        if hit is not None:
            result = hit  # cache hit
        else:
            result = execute_tool(...)
            cache.put("check_weather", {"location": "NYC"}, result)
    """

    def __init__(
        self,
        tool_ttls: dict[str, int] | None = None,
        default_ttl: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ):
        self._tool_ttls = tool_ttls or {}
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._cache: dict[str, CacheEntry] = {}
        self._access_order: list[str] = []  # Most recently accessed last
        self._hits = 0
        self._misses = 0

    def _ttl_for(self, tool_name: str) -> int:
        """Get the TTL in seconds for a given tool."""
        return self._tool_ttls.get(tool_name, self._default_ttl)

    def get(self, tool_name: str, tool_input: dict) -> str | None:
        """Look up a cached result. Returns None on miss or expiry."""
        key = _cache_key(tool_name, tool_input)
        entry = self._cache.get(key)

        if entry is None:
            self._misses += 1
            return None

        if entry.is_expired:
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            self._misses += 1
            logger.debug("Cache expired for %s", tool_name)
            return None

        # Don't serve cached errors.
        if entry.is_error:
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            self._misses += 1
            return None

        # Update access order (LRU).
        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

        self._hits += 1
        logger.debug("Cache hit for %s (age=%.1fs)", tool_name, time.time() - entry.created_at)
        return entry.result

    def put(
        self,
        tool_name: str,
        tool_input: dict,
        result: str,
        is_error: bool = False,
    ) -> None:
        """Store a tool result in the cache."""
        ttl = self._ttl_for(tool_name)
        if ttl <= 0:
            return  # TTL of 0 means no caching for this tool

        key = _cache_key(tool_name, tool_input)
        self._cache[key] = CacheEntry(
            tool_name=tool_name,
            result=result,
            created_at=time.time(),
            ttl_seconds=ttl,
            is_error=is_error,
        )

        if key in self._access_order:
            self._access_order.remove(key)
        self._access_order.append(key)

        # Evict oldest entries if over capacity.
        self._evict()

    def _evict(self) -> None:
        """Remove expired entries and LRU evict if over max_entries."""
        # First pass: remove expired.
        expired_keys = [k for k, v in self._cache.items() if v.is_expired]
        for k in expired_keys:
            del self._cache[k]
            if k in self._access_order:
                self._access_order.remove(k)

        # LRU eviction.
        while len(self._cache) > self._max_entries and self._access_order:
            oldest_key = self._access_order.pop(0)
            self._cache.pop(oldest_key, None)

    def invalidate(self, tool_name: str, tool_input: dict) -> bool:
        """Remove a specific entry from the cache. Returns True if found."""
        key = _cache_key(tool_name, tool_input)
        if key in self._cache:
            del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)
            return True
        return False

    def clear(self) -> None:
        """Clear all cached entries."""
        self._cache.clear()
        self._access_order.clear()

    @property
    def stats(self) -> dict[str, int | float]:
        """Return cache statistics."""
        return {
            "entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate_pct": round(self._hits / max(1, self._hits + self._misses) * 100, 1),
        }
