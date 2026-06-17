"""Tests for intelligence.cache — TTL expiry, hit/miss, clear_expired."""

from datetime import datetime, timedelta

from intelligence.cache import SignalCache
from intelligence.signal import MarketSignal


def _signal(market_id="m1") -> MarketSignal:
    return MarketSignal(
        market_id=market_id,
        market_question="Will X?",
        current_yes_price=0.5,
        ai_probability=0.6,
        confidence=0.7,
        direction="bullish",
        reasoning="r",
        news_headlines=[],
    )


def test_set_and_get_hit():
    cache = SignalCache(ttl_minutes=10)
    cache.set("Fed Rate Decision", _signal())
    # Key normalization: different case / whitespace still hits.
    got = cache.get("  fed rate decision ")
    assert got is not None
    assert got.cache_hit is True


def test_miss_returns_none():
    cache = SignalCache(ttl_minutes=10)
    assert cache.get("nonexistent") is None
    assert cache.stats()["misses"] == 1


def test_ttl_expiry():
    cache = SignalCache(ttl_minutes=10)
    cache.set("topic", _signal())
    # Force the entry to look old by rewinding its insertion time.
    entry = next(iter(cache._store.values()))
    entry.inserted_at = datetime.utcnow() - timedelta(minutes=11)
    assert cache.get("topic") is None  # expired -> miss + eviction
    assert "topic" not in cache._store


def test_clear_expired_count():
    cache = SignalCache(ttl_minutes=10)
    cache.set("fresh", _signal("fresh"))
    cache.set("stale", _signal("stale"))
    cache._store["stale"].inserted_at = datetime.utcnow() - timedelta(minutes=99)

    cleared = cache.clear_expired()
    assert cleared == 1
    assert "fresh" in cache._store
    assert "stale" not in cache._store


def test_stats_hit_rate():
    cache = SignalCache(ttl_minutes=10)
    cache.set("a", _signal())
    cache.get("a")          # hit
    cache.get("missing")    # miss
    stats = cache.stats()
    assert stats["hits"] == 1
    assert stats["misses"] == 1
    assert abs(stats["hit_rate"] - 0.5) < 1e-9
    assert stats["size"] == 1
    assert stats["oldest_entry"] is not None
