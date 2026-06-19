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
"""
from __future__ import annotations

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
    """

    def __init__(
        self,
        kalshi_client,
        categorize_fn,
        min_volume: int,
        exclude_categories: list[str],
    ) -> None:
        self._client = kalshi_client
        self._categorize = categorize_fn
        self._min_volume = min_volume
        self._exclude = set(exclude_categories)

    async def scan(self, max_markets: int) -> list[KalshiMarket]:
        """Fetch, filter, and categorise open markets.

        Calls ``list_all_markets`` (which paginates and excludes parlays),
        then applies local filters and tags each market's ``.category``.
        """
        markets = await self._client.list_all_markets(
            status="open",
            max_markets=max_markets,
        )

        result: list[KalshiMarket] = []
        for market in markets:
            if not is_tradeable(market):
                continue
            if market.volume < self._min_volume:
                continue
            category = self._categorize(market.event_ticker)
            if category in self._exclude:
                continue
            market.category = category
            result.append(market)

        return result
