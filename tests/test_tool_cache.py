"""Tests for tool result caching with TTL."""

from __future__ import annotations

from creel.tool_cache import ToolResultCache, _cache_key

# -- Cache key generation --


def test_cache_key_deterministic():
    key1 = _cache_key("weather", {"location": "NYC"})
    key2 = _cache_key("weather", {"location": "NYC"})
    assert key1 == key2


def test_cache_key_different_inputs():
    key1 = _cache_key("weather", {"location": "NYC"})
    key2 = _cache_key("weather", {"location": "LA"})
    assert key1 != key2


def test_cache_key_different_tools():
    key1 = _cache_key("weather", {"query": "test"})
    key2 = _cache_key("search", {"query": "test"})
    assert key1 != key2


def test_cache_key_order_independent():
    """Dict key order should not affect the cache key."""
    key1 = _cache_key("tool", {"a": "1", "b": "2"})
    key2 = _cache_key("tool", {"b": "2", "a": "1"})
    assert key1 == key2


# -- Basic cache operations --


def test_cache_miss_returns_none():
    cache = ToolResultCache()
    assert cache.get("weather", {"location": "NYC"}) is None


def test_cache_put_and_get():
    cache = ToolResultCache()
    cache.put("weather", {"location": "NYC"}, "Sunny, 72F")
    assert cache.get("weather", {"location": "NYC"}) == "Sunny, 72F"


def test_cache_different_inputs_separate():
    cache = ToolResultCache()
    cache.put("weather", {"location": "NYC"}, "Sunny")
    cache.put("weather", {"location": "LA"}, "Cloudy")
    assert cache.get("weather", {"location": "NYC"}) == "Sunny"
    assert cache.get("weather", {"location": "LA"}) == "Cloudy"


# -- TTL expiry --


def test_cache_ttl_expiry():
    cache = ToolResultCache(default_ttl=1)  # 1 second TTL
    cache.put("weather", {"location": "NYC"}, "Sunny")
    assert cache.get("weather", {"location": "NYC"}) == "Sunny"

    # Simulate time passing by patching the entry's created_at
    key = _cache_key("weather", {"location": "NYC"})
    cache._cache[key].created_at -= 2  # Make it 2 seconds old

    assert cache.get("weather", {"location": "NYC"}) is None


def test_cache_per_tool_ttl():
    cache = ToolResultCache(
        tool_ttls={"weather": 3600, "email": 60},
        default_ttl=300,
    )
    assert cache._ttl_for("weather") == 3600
    assert cache._ttl_for("email") == 60
    assert cache._ttl_for("search") == 300  # default


def test_cache_zero_ttl_disables_caching():
    cache = ToolResultCache(tool_ttls={"no_cache": 0}, default_ttl=300)
    cache.put("no_cache", {"key": "val"}, "result")
    # Should not be cached
    assert cache.get("no_cache", {"key": "val"}) is None


# -- Error handling --


def test_cache_errors_not_stored():
    """Errors should not be stored in the cache at all."""
    cache = ToolResultCache()
    cache.put("weather", {"location": "NYC"}, "Error: timeout", is_error=True)
    assert cache.get("weather", {"location": "NYC"}) is None
    assert cache.stats["entries"] == 0


# -- Invalidation --


def test_cache_invalidate():
    cache = ToolResultCache()
    cache.put("weather", {"location": "NYC"}, "Sunny")
    assert cache.invalidate("weather", {"location": "NYC"}) is True
    assert cache.get("weather", {"location": "NYC"}) is None


def test_cache_invalidate_missing():
    cache = ToolResultCache()
    assert cache.invalidate("weather", {"location": "NYC"}) is False


def test_cache_clear():
    cache = ToolResultCache()
    cache.put("weather", {"location": "NYC"}, "Sunny")
    cache.put("email", {"id": "1"}, "Hello")
    cache.clear()
    assert cache.get("weather", {"location": "NYC"}) is None
    assert cache.get("email", {"id": "1"}) is None


# -- LRU eviction --


def test_cache_lru_eviction():
    cache = ToolResultCache(max_entries=3)
    cache.put("t1", {}, "r1")
    cache.put("t2", {}, "r2")
    cache.put("t3", {}, "r3")
    # Cache is full. Adding one more should evict the oldest.
    cache.put("t4", {}, "r4")
    # t1 was least recently used, should be evicted
    assert cache.get("t1", {}) is None
    assert cache.get("t2", {}) is not None
    assert cache.get("t4", {}) is not None


def test_cache_lru_access_refreshes():
    cache = ToolResultCache(max_entries=3)
    cache.put("t1", {}, "r1")
    cache.put("t2", {}, "r2")
    cache.put("t3", {}, "r3")
    # Access t1 to refresh it
    cache.get("t1", {})
    # Now add t4 — t2 should be evicted (oldest after t1 was refreshed)
    cache.put("t4", {}, "r4")
    assert cache.get("t1", {}) is not None  # refreshed, still here
    assert cache.get("t2", {}) is None  # evicted
    assert cache.get("t3", {}) is not None
    assert cache.get("t4", {}) is not None


# -- Stats --


def test_cache_stats():
    cache = ToolResultCache()
    cache.put("weather", {"loc": "NYC"}, "Sunny")
    cache.get("weather", {"loc": "NYC"})  # hit
    cache.get("weather", {"loc": "LA"})  # miss

    stats = cache.stats
    assert stats["entries"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert stats["hit_rate_pct"] == 50.0
