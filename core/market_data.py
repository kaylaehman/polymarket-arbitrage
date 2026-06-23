"""Financial market data client — Alpha Vantage gated.

Provides AVClient for fetching prices/vol, parse_financial_ticker for Kalshi
financial ticker parsing, and crossing_margin for the z-score gate computation.

Confirmed Kalshi financial series (2026-06-22):
  KXBTC   - Bitcoin price range (B-type buckets, 250-wide)
  KXBTCD  - Bitcoin price threshold (T-type, YES=BTC>=threshold at 5pm EDT)
  KXETH   - Ethereum price range (B-type)
  KXETHD  - Ethereum price threshold (T-type)
  KXWTI   - WTI crude oil threshold (T-type, YES=WTI>=threshold at 2pm EDT)
  KXEURUSD - EUR/USD threshold + bucket (T-type and B-type, 10am EDT)

Excluded series (no free AV mapping): KXDOGE, KXBNB.

AV free tier: 25 calls/day, 1/sec. All calls serialized via asyncio.Lock.
TTL caches: price ~4h (configurable), vol ~24h. Rate-limit/Note/Information
responses silently return None — never raise.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from math import isfinite, sqrt
from statistics import stdev
from typing import Optional

logger = logging.getLogger(__name__)

_SERIES_UNDERLYING: dict[str, str] = {
    "KXBTC":    "BTC",
    "KXBTCD":   "BTC",
    "KXETH":    "ETH",
    "KXETHD":   "ETH",
    "KXWTI":    "WTI",
    "KXEURUSD": "EURUSD",
}

_VOL_FALLBACK: dict[str, float] = {
    "BTC":    0.04,
    "ETH":    0.05,
    "WTI":    0.02,
    "EURUSD": 0.005,
}
_VOL_FALLBACK_DEFAULT = 0.03

_MONTH_MAP: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_TICKER_RE = re.compile(
    r"^(KXBTCD?|KXETHD?|KXWTI|KXEURUSD)-(\d{2})([A-Z]{3})(\d{2})(\d{2})-([TB])(.+)$"
)

AV_BASE = "https://www.alphavantage.co/query"


@dataclass(frozen=True)
class FinancialMarket:
    series: str
    underlying: str
    threshold: float
    direction: str  # "above" | "below"
    expiry: date
    market_type: str  # "threshold" | "bucket"
    bucket_lo: Optional[float] = None
    bucket_hi: Optional[float] = None


def parse_financial_ticker(ticker: str) -> FinancialMarket | None:
    """Parse KXBTC*/KXETH*/KXWTI*/KXEURUSD* tickers. Returns None for non-financial."""
    if not ticker:
        return None
    m = _TICKER_RE.match(ticker)
    if m is None:
        return None
    series, yy, mon, dd, _hour, tb, val_str = m.groups()
    underlying = _SERIES_UNDERLYING.get(series)
    if underlying is None:
        return None
    try:
        month = _MONTH_MAP[mon.upper()]
    except KeyError:
        return None
    year = 2000 + int(yy)
    day = int(dd)
    try:
        expiry = date(year, month, day)
    except ValueError:
        return None
    try:
        threshold = float(val_str)
    except ValueError:
        return None

    if tb == "T":
        return FinancialMarket(
            series=series,
            underlying=underlying,
            threshold=threshold,
            direction="above",
            expiry=expiry,
            market_type="threshold",
        )
    else:  # B-type bucket
        return FinancialMarket(
            series=series,
            underlying=underlying,
            threshold=threshold,
            direction="above",
            expiry=expiry,
            market_type="bucket",
            bucket_lo=threshold,
        )


def crossing_margin(price: float, vol: float, threshold: float, days: float) -> float:
    """Compute z-score: how many expected-moves is the threshold from current price.

    expected_move = price * vol * sqrt(max(days, 1))
    z = (threshold - price) / expected_move

    Positive z: threshold is ABOVE price (safe for NO-bet on 'above' market).
    Negative z: threshold is BELOW price (dangerous for NO-bet on 'above' market).
    """
    em = price * vol * sqrt(max(days, 1.0))
    if em <= 0:
        return float("inf")
    return (threshold - price) / em


class AVClient:
    """Alpha Vantage HTTP client with TTL cache and 1-req/sec rate-limit."""

    def __init__(self, api_key: str, price_ttl_s: int, vol_ttl_s: int, *, http) -> None:
        self._api_key = api_key
        self._price_ttl_s = price_ttl_s
        self._vol_ttl_s = vol_ttl_s
        self._http = http
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0
        self._price_cache: dict[str, tuple[float, float]] = {}
        self._vol_cache: dict[str, tuple[float, float]] = {}
        self._warned_rate: set[str] = set()

    async def _fetch(self, params: dict) -> dict | None:
        """Acquire lock, throttle to 1/sec, fetch JSON. Returns None on any error."""
        async with self._lock:
            now = time.monotonic()
            wait = 1.0 - (now - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._http.get(AV_BASE, params={**params, "apikey": self._api_key})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[av] HTTP error: %s", exc)
                return None
            finally:
                self._last_call = time.monotonic()

        # Rate-limit detection
        if "Note" in data or "Information" in data:
            key = params.get("function", "?")
            if key not in self._warned_rate:
                self._warned_rate.add(key)
                logger.warning("[av] Rate-limited on %s — returning None", key)
            return None
        return data

    async def get_price(self, underlying: str) -> float | None:
        """Fetch spot price for underlying. Returns cached value within TTL."""
        now = time.time()
        cached = self._price_cache.get(underlying)
        if cached is not None and (now - cached[1]) < self._price_ttl_s:
            return cached[0]

        price: float | None = None

        if underlying in ("BTC", "ETH"):
            data = await self._fetch({
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": underlying,
                "to_currency": "USD",
            })
            if data:
                try:
                    rate_block = data.get("Realtime Currency Exchange Rate", {})
                    price = float(rate_block["5. Exchange Rate"])
                except (KeyError, ValueError, TypeError):
                    pass

        elif underlying == "EURUSD":
            data = await self._fetch({
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": "EUR",
                "to_currency": "USD",
            })
            if data:
                try:
                    rate_block = data.get("Realtime Currency Exchange Rate", {})
                    price = float(rate_block["5. Exchange Rate"])
                except (KeyError, ValueError, TypeError):
                    pass

        elif underlying == "WTI":
            data = await self._fetch({"function": "WTI", "interval": "daily"})
            if data:
                try:
                    pts = data.get("data", [])
                    for pt in pts:
                        v = pt.get("value", ".")
                        if v != ".":
                            price = float(v)
                            break
                except (KeyError, ValueError, TypeError):
                    pass

        else:
            logger.debug("[av] Unknown underlying %s, cannot fetch price", underlying)

        if price is not None:
            self._price_cache[underlying] = (price, now)
        return price

    async def daily_vol(self, underlying: str) -> float:
        """Return stdev of ~20 daily % returns. Falls back to per-asset default."""
        now = time.time()
        cached = self._vol_cache.get(underlying)
        if cached is not None and (now - cached[1]) < self._vol_ttl_s:
            return cached[0]

        vol: float | None = None

        if underlying in ("BTC", "ETH"):
            data = await self._fetch({
                "function": "DIGITAL_CURRENCY_DAILY",
                "symbol": underlying,
                "market": "USD",
            })
            if data:
                try:
                    ts = data.get("Time Series (Digital Currency Daily)", {})
                    sorted_dates = sorted(ts.keys(), reverse=True)[:21]
                    prices = [float(ts[d]["4. close"]) for d in sorted_dates]
                    if len(prices) >= 2:
                        returns = [prices[i] / prices[i + 1] - 1 for i in range(len(prices) - 1)]
                        if len(returns) >= 2:
                            vol = stdev(returns)
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    pass

        elif underlying == "EURUSD":
            data = await self._fetch({
                "function": "FX_DAILY",
                "from_symbol": "EUR",
                "to_symbol": "USD",
            })
            if data:
                try:
                    ts = data.get("Time Series FX (Daily)", {})
                    sorted_dates = sorted(ts.keys(), reverse=True)[:21]
                    prices = [float(ts[d]["4. close"]) for d in sorted_dates]
                    if len(prices) >= 2:
                        returns = [prices[i] / prices[i + 1] - 1 for i in range(len(prices) - 1)]
                        if len(returns) >= 2:
                            vol = stdev(returns)
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    pass

        elif underlying == "WTI":
            data = await self._fetch({"function": "WTI", "interval": "daily"})
            if data:
                try:
                    pts = [p for p in data.get("data", []) if p.get("value", ".") != "."][:21]
                    prices = [float(p["value"]) for p in pts]
                    if len(prices) >= 2:
                        returns = [prices[i] / prices[i + 1] - 1 for i in range(len(prices) - 1)]
                        if len(returns) >= 2:
                            vol = stdev(returns)
                except (KeyError, ValueError, TypeError, ZeroDivisionError):
                    pass

        if vol is None or not isfinite(vol) or vol <= 0:
            vol = _VOL_FALLBACK.get(underlying, _VOL_FALLBACK_DEFAULT)
            logger.debug("[av] Using fallback vol %.4f for %s", vol, underlying)

        self._vol_cache[underlying] = (vol, now)
        return vol
