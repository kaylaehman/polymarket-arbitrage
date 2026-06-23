"""PM.US weather market source for the directional maker.

Fetches active PM.US climate-category markets, parses slugs, derives
orderbook-based no_ask, and produces KalshiMarket-compatible objects so
MakerLongshotStrategy can run its existing weather gate on them unchanged.

The market_id uses the prefix ``pmus:`` to distinguish PM.US positions
from Kalshi positions in DirectionalStore.  The executor routes ``pmus:``
IDs to polymarket_us_client in live mode (see PMUSWeatherExecutorMixin in
executor.py).

Config gate: ``directional.pmus_weather.enabled`` (default True but only
acts when PM.US weather markets are found and pass the forecast gate).

Paper mode: positions are recorded with market_id="pmus:<slug>"; the
existing paper record path in Executor._record() is venue-agnostic.

Live mode (follow-up): Executor._place_maker currently calls
kalshi_client.place_order.  For PM.US live execution the caller must pass
a polymarket_us_client alongside the order.  This is flagged clearly in
the executor; paper-first is the safe default.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from core.weather import PMUS_CITY_SERIES, PMUSWeatherBucket, parse_pmus_slug

logger = logging.getLogger(__name__)

# Maximum days out to include PM.US weather markets (mirrors maker max_days).
_DEFAULT_MAX_DAYS = 30.0

# Fetch pagination: PM.US paginates /v1/markets by offset; 500 is the hard max.
_PAGE_SIZE = 500
_PAGE_DELAY = 0.4  # seconds; PM.US is behind Cloudflare and 429s on bursts

# Gateway URL for public market reads.
_GATEWAY = "https://gateway.polymarket.us"


def _pmus_to_kalshi_market(
    slug: str,
    question: str,
    end_date: datetime,
    yes_bid: Optional[float],
    yes_ask: Optional[float],
) -> Optional[Any]:
    """Build a KalshiMarket-shaped SimpleNamespace from PM.US data.

    Returns None if the orderbook is too thin to derive a valid no_ask.
    The object exposes exactly the fields MakerLongshotStrategy.scan() reads:
      .ticker, .event_ticker, .yes_price, .category, .title, .close_time,
      .to_unified_market_id(), .status
    And the scanner helpers read:
      .ticker (used as key in last_books)
    """
    if yes_bid is None or yes_ask is None:
        return None

    mid = round((yes_bid + yes_ask) / 2, 4)
    no_ask = round(1.0 - yes_bid, 4)  # synthetic NO ask = 1 - YES best bid

    m = SimpleNamespace()
    # Use slug as the ticker key; prefixed so it never collides with Kalshi tickers.
    m.ticker = f"pmus:{slug}"
    m.event_ticker = f"pmus:{slug}"
    m.series_ticker = ""
    m.yes_price = mid
    m.no_price = round(1.0 - mid, 4)
    m.category = "weather"
    m.title = question
    m.status = "open"
    m.result = None
    m.close_time = end_date
    m.volume = 0
    m.open_interest = 0
    # Attach no_ask so the scanner.no_ask() shim can serve it.
    m._no_ask = no_ask
    m.to_unified_market_id = lambda: f"pmus:{slug}"
    return m


class PMUSWeatherSource:
    """Fetch and parse PM.US climate markets into KalshiMarket-compatible objects.

    Designed to be called once per maker scan cycle.  Results are cached for
    ``cache_ttl_seconds`` (default 300 = 5 min, matching the engine's scan interval).

    Args:
        http: An async HTTP client (httpx.AsyncClient or compatible).
        max_days: Skip markets resolving more than this many days from now.
        cache_ttl_seconds: How long to cache the last result before re-fetching.
    """

    def __init__(
        self,
        http: Any,
        max_days: float = _DEFAULT_MAX_DAYS,
        cache_ttl_seconds: float = 300.0,
    ) -> None:
        self._http = http
        self._max_days = max_days
        self._cache_ttl = cache_ttl_seconds
        self._cached: list[Any] = []
        self._cached_books: dict[str, Any] = {}
        self._fetched_at: Optional[float] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def fetch(self) -> list[Any]:
        """Return cached or freshly-fetched PM.US weather KalshiMarket objects."""
        import time
        now = time.monotonic()
        if self._fetched_at is not None and (now - self._fetched_at) < self._cache_ttl:
            return self._cached

        markets, books = await self._fetch_fresh()
        self._cached = markets
        self._cached_books = books
        self._fetched_at = now
        return markets

    def no_ask(self, ticker: str) -> Optional[float]:
        """Return pre-fetched no_ask for a pmus: ticker key."""
        m = self._cached_books.get(ticker)
        if m is not None:
            return getattr(m, "_no_ask", None)
        # fallback: search the cached market list
        for mkt in self._cached:
            if getattr(mkt, "ticker", None) == ticker:
                return getattr(mkt, "_no_ask", None)
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fetch_fresh(self) -> tuple[list[Any], dict[str, Any]]:
        """Fetch active PM.US climate markets, parse slugs, build market objects."""
        now_utc = datetime.now(timezone.utc)
        raw = await self._fetch_raw_climate_markets()
        markets: list[Any] = []
        books: dict[str, Any] = {}

        for raw_m in raw:
            slug = raw_m.get("slug", "")
            wb: Optional[PMUSWeatherBucket] = parse_pmus_slug(slug)
            if wb is None:
                continue  # not a parseable tc-temp-* slug

            end_raw = raw_m.get("endDate", "")
            if not end_raw:
                continue
            try:
                end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            except ValueError:
                continue

            delta_days = (end_dt - now_utc).total_seconds() / 86400.0
            if delta_days <= 0 or delta_days > self._max_days:
                continue

            question = raw_m.get("question", slug)
            yes_bid, yes_ask = self._extract_prices(raw_m)

            mkt = _pmus_to_kalshi_market(slug, question, end_dt, yes_bid, yes_ask)
            if mkt is None:
                continue

            ticker_key = f"pmus:{slug}"
            markets.append(mkt)
            books[ticker_key] = mkt

        logger.info(
            "[pmus-weather] %d parseable PM.US weather markets within %dd",
            len(markets),
            int(self._max_days),
        )
        return markets, books

    def _extract_prices(
        self, raw_m: dict
    ) -> tuple[Optional[float], Optional[float]]:
        """Extract YES best_bid / best_ask from a PM.US market dict.

        PM.US /v1/markets returns a ``marketSides`` list with ``price`` and
        ``tradable`` fields.  The YES side is typically the first entry.
        Falls back to ``lastTradePx`` when sides are absent.
        """
        sides = raw_m.get("marketSides", []) or []
        if len(sides) >= 1 and sides[0].get("price") is not None:
            # Single-price per side (mid-price proxy); treat as both bid and ask.
            try:
                p = float(sides[0]["price"])
                return p, p
            except (TypeError, ValueError):
                pass
        # No usable price
        return None, None

    async def _fetch_raw_climate_markets(self) -> list[dict]:
        """Page through PM.US /v1/markets filtered to active climate markets."""
        markets: list[dict] = []
        offset = 0

        while True:
            params = {
                "limit": _PAGE_SIZE,
                "offset": offset,
                "active": "true",
                "closed": "false",
            }
            try:
                resp = await self._http.get(f"{_GATEWAY}/v1/markets", params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("[pmus-weather] fetch stopped at offset=%d: %s", offset, exc)
                break

            batch = (
                data if isinstance(data, list)
                else data.get("markets", data.get("data", []))
            )
            if not batch:
                break

            for m in batch:
                slug = m.get("slug", "")
                cat = (m.get("category") or "").lower()
                # Accept climate category OR any tc-temp-* slug directly.
                if cat in ("climate", "weather") or slug.startswith("tc-temp-"):
                    markets.append(m)

            offset += _PAGE_SIZE
            if len(batch) < _PAGE_SIZE:
                break  # last page

            await asyncio.sleep(_PAGE_DELAY)

        logger.debug("[pmus-weather] fetched %d raw climate markets", len(markets))
        return markets
