"""Kalshi market scanner for directional trading.

Wraps the existing ``kalshi_client.list_all_markets`` which already paginates
and strips KXMV parlay series.  This scanner adds three additional filters:

1. ``is_tradeable`` — secondary guard using yes_price / no_price as *last-price*
   proxies (not ask prices).  The plan explicitly notes these are last prices, not
   ask prices; the primary parlay/KXMV exclusion is already handled upstream.
2. Volume floor — markets below ``min_volume`` are noisy and illiquid.
3. Category exclude — caller-supplied list of category strings to skip.

Finally, each surviving market is tagged with its category via ``categorize_fn``
so downstream strategies never need to call the categorizer themselves.

Fix 1: After filtering, results are sorted by volume descending and capped to
``max_markets`` so the engine never fetches orderbooks for hundreds of markets.

Fix 2: The raw market list is cached for ``cache_ttl_seconds`` to avoid
re-fetching ~570 markets on every cycle.  Filtering and capping are applied fresh
on every ``scan`` call; only the upstream API call is cached.
"""
from __future__ import annotations

import time
from typing import Callable, List, Optional

from kalshi_client.models import KalshiMarket


def is_tradeable(market: KalshiMarket) -> bool:
    """Return True if the market appears to be actively tradeable.

    Uses yes_price / no_price as last-price proxies (secondary guard — the
    primary KXMV/parlay filter is upstream in list_all_markets).

    Rejects when:
    - yes_price is missing/zero (no last-trade data).
    - no_price is missing/zero.
    - Both prices implausibly close to 1.0 (settlement collection artifact).
    """
    if not market.yes_price or not market.no_price:
        return False
    if market.yes_price >= 0.99 and market.no_price >= 0.99:
        return False
    return True


class KalshiMarketScanner:
    """Scan open Kalshi markets and return a filtered, categorised list.

    Args:
        kalshi_client: Any object exposing
            ``async list_all_markets(status, max_markets) -> list[KalshiMarket]``.
            Reuses the existing client — do NOT add a second method there.
        categorize_fn: Callable ``(event_ticker: str) -> str``; typically
            ``utils.kalshi_categories.categorize``.
        min_volume: Minimum ``market.volume`` to pass the filter.
        exclude_categories: Category strings to skip entirely.
        cache_ttl_seconds: How long (seconds) to reuse the raw market list from
            ``list_all_markets`` before re-fetching.  Default 600 (10 minutes).
            Filtering and capping are always applied fresh; only the API call is
            cached.
        _now_fn: Optional callable returning a monotonic float (seconds).  Defaults
            to ``time.monotonic``.  Inject a fake clock in tests.
    """

    def __init__(
        self,
        kalshi_client,
        categorize_fn: Callable[[str], str],
        min_volume: int,
        exclude_categories: List[str],
        cache_ttl_seconds: int = 600,
        _now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._client = kalshi_client
        self._categorize = categorize_fn
        self._min_volume = min_volume
        self._exclude = set(exclude_categories)
        self._cache_ttl = cache_ttl_seconds
        self._now = _now_fn if _now_fn is not None else time.monotonic
        self._cached_markets: List[KalshiMarket] = []
        self._fetched_at: Optional[float] = None

    async def scan(self, max_markets: int) -> List[KalshiMarket]:
        """Fetch (or serve from cache), filter, cap, and categorise open markets.

        The raw market list from ``list_all_markets`` is cached for
        ``cache_ttl_seconds``.  Filtering and the ``max_markets`` cap are applied
        on every call so changing ``max_markets`` between cycles always works.

        Returns markets sorted by volume descending, capped to ``max_markets``.
        """
        now = self._now()
        cache_valid = (
            self._fetched_at is not None
            and (now - self._fetched_at) < self._cache_ttl
        )

        if not cache_valid:
            self._cached_markets = await self._client.list_all_markets(
                status="open",
                max_markets=max_markets,
            )
            self._fetched_at = now

        result: List[KalshiMarket] = []
        for market in self._cached_markets:
            if not is_tradeable(market):
                continue
            if market.volume < self._min_volume:
                continue
            category = self._categorize(market.event_ticker)
            if category in self._exclude:
                continue
            market.category = category
            result.append(market)

        result.sort(key=lambda m: m.volume, reverse=True)
        return result[:max_markets]
