"""
music_intel.ratelimit — Polite async rate limiter for chart data sources.

Mirrors the AVClient per-call throttle but is host-keyed so multiple
sources with different politeness budgets share one limiter instance.

Usage::

    limiter = RateLimiter(min_interval=2.0, max_calls_per_day=500)
    await limiter.acquire("kworb.net")
    response = await http.get(url)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_DEFAULT_MIN_INTERVAL = 2.0  # seconds between calls to the same host
_DEFAULT_MAX_CALLS_PER_DAY = 500


class RateLimiter:
    """Async per-host rate limiter with a daily call cap.

    ``acquire(host)`` blocks until at least ``min_interval`` seconds have
    elapsed since the last call to that host, then checks the daily cap.
    On cap-exceeded the call returns immediately (callers should treat the
    subsequent fetch as an expected degraded path and return ``[]``).
    """

    def __init__(
        self,
        min_interval: float = _DEFAULT_MIN_INTERVAL,
        max_calls_per_day: int = _DEFAULT_MAX_CALLS_PER_DAY,
    ) -> None:
        self._min_interval = min_interval
        self._max_calls_per_day = max_calls_per_day
        # per-host: last call timestamp (monotonic)
        self._last_call: dict[str, float] = {}
        # per-host lock so concurrent acquires for the same host queue up
        self._host_locks: dict[str, asyncio.Lock] = {}
        # daily counters (reset when UTC date rolls over)
        self._daily_calls: int = 0
        self._call_date: str = ""
        self._cap_warned: bool = False

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._call_date:
            self._call_date = today
            self._daily_calls = 0
            self._cap_warned = False

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def acquire(self, host: str) -> bool:
        """Block until it is polite to make a request to *host*.

        Returns:
            ``True`` if the caller may proceed, ``False`` if the daily cap
            has been reached (caller should skip the request).
        """
        self._reset_daily_if_needed()

        if self._daily_calls >= self._max_calls_per_day:
            if not self._cap_warned:
                self._cap_warned = True
                logger.warning(
                    "[ratelimit] Daily cap of %d reached — skipping %s",
                    self._max_calls_per_day,
                    host,
                )
            return False

        async with self._lock_for(host):
            now = time.monotonic()
            last = self._last_call.get(host, 0.0)
            wait = self._min_interval - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[host] = time.monotonic()
            self._daily_calls += 1

        return True
