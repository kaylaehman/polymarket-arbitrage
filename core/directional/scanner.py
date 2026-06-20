"""Kalshi market scanner for directional trading.

Redesigned to use the Kalshi EVENTS endpoint (with nested markets) as the
universe source instead of list_all_markets.  This endpoint returns a broad
set of real open binary markets that have live orderbook data.

Pipeline:
1. UNIVERSE  — fetch via /events?status=open&with_nested_markets=true, paginate
               1-2 pages (up to ~400 events → many more markets).  Flatten nested
               ``market`` arrays into KalshiMarket objects.  Skip parlays
               (tickers starting with "KXMV" or containing "MULTIGAME"/"MULTIMARKET").
               Cache the flat universe for ``cache_ttl_seconds``.

2. PROBE      — for each candidate (capped to max(60, max_markets*3)) call
               ``get_orderbook_unified(ticker)``.  KEEP only markets with a real
               two-sided YES book: ``ob.yes.best_bid`` and ``ob.yes.best_ask`` are
               both not None AND spread ≤ MAX_SPREAD.  Attach the mid-price as
               ``market.yes_price``.  Store the fetched books in ``self.last_books``
               so the engine never re-fetches them.

3. CATEGORISE — tag with ``categorize_fn(event_ticker)``; drop excluded categories.

4. RETURN     — up to ``max_markets`` markets, sorted by tightest YES spread first.

The ``min_volume`` constructor param is accepted for backwards compatibility with
the engine's constructor call but is otherwise unused (the /events endpoint does
not return volume data either).
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

from kalshi_client.models import KalshiMarket

logger = logging.getLogger(__name__)

# Default maximum YES spread (ask - bid) to consider a market liquid.
# Can be overridden per-instance via the max_spread constructor parameter.
MAX_SPREAD = 0.20

# Parlay ticker prefixes/substrings to exclude.
_PARLAY_PREFIX = "KXMV"
_PARLAY_SUBSTRINGS = ("MULTIGAME", "MULTIMARKET")


def _is_parlay(ticker: str) -> bool:
    t = ticker.upper()
    if t.startswith(_PARLAY_PREFIX):
        return True
    return any(sub in t for sub in _PARLAY_SUBSTRINGS)


def _parse_market_from_dict(data: dict) -> Optional[KalshiMarket]:
    """Build a KalshiMarket from a raw API dict (nested-markets format).

    The /events endpoint omits volume and last prices — those fields will be
    0 / None until populated from the orderbook probe in scan().
    """
    ticker = data.get("ticker", "")
    if not ticker:
        return None
    try:
        from datetime import datetime
        close_time = None
        if data.get("close_time"):
            try:
                close_time = datetime.fromisoformat(
                    data["close_time"].replace("Z", "+00:00")
                )
            except Exception:
                pass
        return KalshiMarket(
            ticker=ticker,
            event_ticker=data.get("event_ticker", ""),
            series_ticker=data.get("series_ticker", ""),
            title=data.get("title", ""),
            subtitle=data.get("subtitle", ""),
            yes_price=0.0,
            no_price=0.0,
            status=data.get("status", "open"),
            result=data.get("result"),
            volume=data.get("volume", 0) or 0,
            open_interest=data.get("open_interest", 0) or 0,
            close_time=close_time,
            category="",
        )
    except Exception as exc:
        logger.debug("Failed to parse market dict %s: %s", ticker, exc)
        return None


class KalshiMarketScanner:
    """Scan open Kalshi markets and return a filtered, liquid, categorised list.

    Args:
        kalshi_client: Exposes ``async _get(endpoint, params) -> dict`` and
            ``async get_orderbook_unified(ticker) -> OrderBook | None``.
        categorize_fn: ``(event_ticker: str) -> str``.
        min_volume: Accepted for backwards compatibility; not used in filtering
            because the /events endpoint omits volume.  Pass 0 or any int.
        exclude_categories: Category strings to skip entirely.
        cache_ttl_seconds: How long (seconds) to reuse the raw universe from
            /events before re-fetching.  Orderbook probes run every scan().
        _now_fn: Optional injectable clock (for tests).
    """

    def __init__(
        self,
        kalshi_client,
        categorize_fn: Callable[[str], str],
        min_volume: int,
        exclude_categories: List[str],
        cache_ttl_seconds: int = 600,
        _now_fn: Optional[Callable[[], float]] = None,
        max_spread: float = MAX_SPREAD,
    ) -> None:
        self._client = kalshi_client
        self._categorize = categorize_fn
        self._min_volume = min_volume          # kept but unused in filtering
        self._exclude = set(exclude_categories)
        self._cache_ttl = cache_ttl_seconds
        self._now = _now_fn if _now_fn is not None else time.monotonic
        self._max_spread = max_spread

        # Universe cache (flat KalshiMarket list from /events)
        self._cached_universe: List[KalshiMarket] = []
        self._fetched_at: Optional[float] = None

        # Per-scan orderbook results (cleared and repopulated each scan)
        self.last_books: Dict[str, object] = {}  # ticker → OrderBook

    # ------------------------------------------------------------------
    # Public helpers consumed by the engine
    # ------------------------------------------------------------------

    def no_ask(self, ticker: str) -> Optional[float]:
        """Return the NO best_ask from the most-recent scan, or None."""
        ob = self.last_books.get(ticker)
        if ob is None:
            return None
        no_side = getattr(ob, "no", None)
        if no_side is None:
            return None
        return getattr(no_side, "best_ask", None)

    # ------------------------------------------------------------------
    # Universe fetch
    # ------------------------------------------------------------------

    async def _fetch_universe(self) -> List[KalshiMarket]:
        """Fetch up to 2 pages from /events and flatten nested markets."""
        markets: List[KalshiMarket] = []
        seen: set = set()
        cursor: Optional[str] = None

        for _page in range(2):
            params: dict = {
                "status": "open",
                "with_nested_markets": "true",
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor

            try:
                data = await self._client._get("/events", params)
            except Exception as exc:
                logger.warning("Failed to fetch /events page %d: %s", _page, exc)
                break

            events = data.get("events") or []
            for evt in events:
                for m_raw in evt.get("markets") or []:
                    m = _parse_market_from_dict(m_raw)
                    if m is None or m.ticker in seen:
                        continue
                    if _is_parlay(m.ticker):
                        continue
                    seen.add(m.ticker)
                    markets.append(m)

            cursor = data.get("cursor")
            if not cursor:
                break

        logger.info("[scanner] Universe: %d non-parlay markets from /events", len(markets))
        return markets

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    async def scan(self, max_markets: int) -> List[KalshiMarket]:
        """Return up to ``max_markets`` liquid, categorised markets.

        Steps:
        1. Refresh universe from /events if cache is stale.
        2. Probe orderbooks for up to max(60, max_markets*3) candidates.
        3. Filter to markets with real two-sided YES books and tight spreads.
        4. Tag category; drop excluded categories.
        5. Sort by YES spread (tightest first) and cap to max_markets.
        """
        # 1. Universe (cached)
        now = self._now()
        cache_valid = (
            self._fetched_at is not None
            and (now - self._fetched_at) < self._cache_ttl
        )
        if not cache_valid:
            self._cached_universe = await self._fetch_universe()
            self._fetched_at = now

        # 2. Probe orderbooks — clear stale books
        self.last_books = {}
        probe_limit = max(60, max_markets * 3)
        candidates = self._cached_universe[:probe_limit]

        liquid: List[KalshiMarket] = []
        for market in candidates:
            try:
                ob = await self._client.get_orderbook_unified(market.ticker)
            except Exception as exc:
                logger.debug("[scanner] orderbook error %s: %s", market.ticker, exc)
                continue

            if ob is None:
                continue
            yes = getattr(ob, "yes", None)
            if yes is None:
                continue

            bid = getattr(yes, "best_bid", None)
            ask = getattr(yes, "best_ask", None)
            if bid is None or ask is None:
                continue
            spread = ask - bid
            if spread > self._max_spread:
                continue

            # Attach real mid-price and store book
            market.yes_price = round((bid + ask) / 2, 4)
            market.no_price = round(1.0 - market.yes_price, 4)
            self.last_books[market.ticker] = ob
            liquid.append(market)

        # 3-4. Categorise and drop excluded categories
        result: List[KalshiMarket] = []
        for market in liquid:
            category = self._categorize(market.event_ticker)
            if category in self._exclude:
                continue
            market.category = category
            result.append(market)

        # 5. Sort tightest spread first, cap
        def _spread(m: KalshiMarket) -> float:
            ob = self.last_books.get(m.ticker)
            if ob is None:
                return 1.0
            yes = getattr(ob, "yes", None)
            if yes is None:
                return 1.0
            bid = getattr(yes, "best_bid", None)
            ask = getattr(yes, "best_ask", None)
            if bid is None or ask is None:
                return 1.0
            return ask - bid

        result.sort(key=_spread)
        return result[:max_markets]
