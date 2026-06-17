"""
TTL Cache
=========

Simple in-memory TTL cache for ``MarketSignal`` objects. No Redis, no deps.

The cache is the primary defense against NewsAPI/Claude rate limits: a topic that
has already been analyzed within ``ttl_minutes`` is served from memory instead of
triggering another news fetch + Claude call.

Cache key = normalized topic string (lowercase, stripped). We deliberately do NOT
key on market_id, because the same real-world event appears under different IDs
across Polymarket and Kalshi — keying on the topic lets one analysis serve both.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from intelligence.signal import MarketSignal


@dataclass
class _Entry:
    signal: MarketSignal
    inserted_at: datetime


class SignalCache:
    """In-memory TTL cache keyed by normalized topic string."""

    def __init__(self, ttl_minutes: int = 10):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._store: dict[str, _Entry] = {}
        self._hits = 0
        self._misses = 0

    @staticmethod
    def normalize_key(topic: str) -> str:
        """Normalize a topic into a stable cache key."""
        return topic.lower().strip()

    def get(self, key: str) -> MarketSignal | None:
        """Return a cached signal if present and not expired, else None."""
        norm = self.normalize_key(key)
        entry = self._store.get(norm)

        if entry is None:
            self._misses += 1
            return None

        if self._is_expired(entry):
            # Lazily evict the stale entry.
            del self._store[norm]
            self._misses += 1
            return None

        self._hits += 1
        # Return a copy-flavored signal flagged as a cache hit so callers/logs
        # can distinguish fresh analyses from cached ones.
        entry.signal.cache_hit = True
        return entry.signal

    def set(self, key: str, signal: MarketSignal) -> None:
        """Insert or overwrite a signal for ``key``."""
        norm = self.normalize_key(key)
        self._store[norm] = _Entry(signal=signal, inserted_at=datetime.utcnow())

    def clear_expired(self) -> int:
        """Evict all expired entries. Returns the number cleared."""
        expired = [k for k, e in self._store.items() if self._is_expired(e)]
        for k in expired:
            del self._store[k]
        return len(expired)

    def stats(self) -> dict:
        """Return cache statistics for logging / the dashboard."""
        total = self._hits + self._misses
        oldest = min((e.inserted_at for e in self._store.values()), default=None)
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0.0,
            "oldest_entry": oldest.isoformat() if oldest else None,
        }

    def _is_expired(self, entry: _Entry) -> bool:
        return datetime.utcnow() - entry.inserted_at > self.ttl
