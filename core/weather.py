"""Forecast-gated weather strategy helpers.

Provides ticker parsing, NWS forecast fetching, and a margin computation
used by MakerLongshotStrategy to gate weather-market NO bets.

Market types on Kalshi KXHIGH* series (confirmed 2026-06-22)
------------------------------------------------------------
  T<num> with ">": YES wins if high > num (HOT bet).  NO favoured when
                   forecast < threshold.  This is the only type gated here.
  T<num> with "<": YES wins if high < num (COLD bet).  Not gated — we do
                   not bet NO on cold-side markets (NO = hot; same risk).
  B<num>:          Range bucket ("78-79°").  YES wins if high in [lo, hi].
                   Semantics (confirmed 2026-06-22): B78.5 → [78, 79],
                   i.e. lo = int(num - 0.5), hi = lo + 1 (always 1° wide).
                   NO bet is safe only when forecast is >= safe_margin_f
                   degrees OUTSIDE the bucket on either side.

Only T-type markets where WeatherMarket.is_above_threshold is True, and
B-type WeatherBucket markets, are forecast-gated.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ticker parsing
# ---------------------------------------------------------------------------

# Matches: KXHIGHNY-26JUN23-T85  (T-type threshold markets)
_TICKER_RE = re.compile(
    r"^(KXHIGH[A-Z]+)"
    r"-(\d{2})([A-Z]{3})(\d{2})"
    r"-T(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)

# Matches: KXHIGHNY-26JUN23-B78.5  (B-type bucket markets)
_BUCKET_RE = re.compile(
    r"^(KXHIGH[A-Z]+)"
    r"-(\d{2})([A-Z]{3})(\d{2})"
    r"-B(\d+(?:\.\d+)?)$",
    re.IGNORECASE,
)

_MONTH_MAP: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass(frozen=True)
class WeatherMarket:
    """Parsed representation of a KXHIGH* T-type market ticker."""

    series: str       # e.g. "KXHIGHNY"
    date: date        # resolution date
    threshold: float  # boundary temperature (degF)
    direction: str    # "above" (YES if high>threshold) or "below" (YES if high<threshold)

    @property
    def is_above_threshold(self) -> bool:
        """True when YES wins on an unusually HOT day (the longshot-NO case)."""
        return self.direction == "above"


@dataclass(frozen=True)
class WeatherBucket:
    """Parsed representation of a KXHIGH* B-type bucket market ticker.

    Confirmed semantics (2026-06-22): B78.5 → "78-79°", i.e. lo=78, hi=79.
    Formula: lo = int(num - 0.5), hi = lo + 1.  Always a 1° wide interval.
    YES wins if the daily high lands in [lo, hi] inclusive.
    """

    series: str   # e.g. "KXHIGHNY"
    date: date    # resolution date
    lo: int       # lower bound of bucket (inclusive), e.g. 78
    hi: int       # upper bound of bucket (inclusive), e.g. 79


# Approximate mid-season daily high (degF) — used ONLY to classify above/below.
# Within +-5F of truth is sufficient; two T markets are issued per series per day.
_SERIES_TYPICAL_HIGH: dict[str, float] = {
    "KXHIGHNY":  80.0,
    "KXHIGHCHI": 73.0,
    "KXHIGHLAX": 72.0,
    "KXHIGHMIA": 90.0,
}


def _parse_date_parts(yy: int, mon_str: str, dd: int) -> Optional[date]:
    """Convert (yy, MON, dd) to a date; None if invalid."""
    month = _MONTH_MAP.get(mon_str.upper())
    if month is None:
        return None
    try:
        return date(2000 + yy, month, dd)
    except ValueError:
        return None


def parse_weather_ticker(ticker: str) -> Optional[WeatherMarket]:
    """Parse a KXHIGH* T-type ticker into a WeatherMarket; None for anything else.

    B-type bucket tickers (e.g. KXHIGHNY-26JUN23-B78.5) return None here;
    use parse_bucket_ticker() for those.
    """
    if not ticker:
        return None
    # Reject B-type buckets before running the full regex
    if re.search(r"-B\d", ticker, re.IGNORECASE):
        return None

    m = _TICKER_RE.match(ticker)
    if m is None:
        return None

    series = m.group(1).upper()
    resolution_date = _parse_date_parts(int(m.group(2)), m.group(3), int(m.group(4)))
    if resolution_date is None:
        return None
    threshold = float(m.group(5))

    typical = _SERIES_TYPICAL_HIGH.get(series, 80.0)
    direction = "above" if threshold >= typical else "below"

    return WeatherMarket(series=series, date=resolution_date, threshold=threshold, direction=direction)


def parse_bucket_ticker(ticker: str) -> Optional[WeatherBucket]:
    """Parse a KXHIGH* B-type bucket ticker into a WeatherBucket; None for anything else.

    Confirmed semantics (2026-06-22): B78.5 → "78-79°" bucket.
    Formula: lo = int(num - 0.5), hi = lo + 1.
    """
    if not ticker:
        return None

    m = _BUCKET_RE.match(ticker)
    if m is None:
        return None

    series = m.group(1).upper()
    resolution_date = _parse_date_parts(int(m.group(2)), m.group(3), int(m.group(4)))
    if resolution_date is None:
        return None

    num = float(m.group(5))
    lo = int(num - 0.5)
    hi = lo + 1

    return WeatherBucket(series=series, date=resolution_date, lo=lo, hi=hi)


# ---------------------------------------------------------------------------
# Station map — confirmed against Kalshi rules_primary text (2026-06-22)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Station:
    label: str
    lat: float
    lon: float


SERIES_STATION: dict[str, Station] = {
    "KXHIGHNY":  Station("Central Park, NY",            40.7829, -73.9654),
    "KXHIGHCHI": Station("Chicago Midway Airport, IL",  41.7868, -87.7522),
    "KXHIGHLAX": Station("Los Angeles Airport, CA",     33.9425, -118.4081),
    "KXHIGHMIA": Station("Miami International Airport", 25.7959, -80.2870),
}

# ---------------------------------------------------------------------------
# NWS forecast cache (per gridpoint forecast URL; TTL 15 min)
# ---------------------------------------------------------------------------

import time as _time

_forecast_cache: dict[str, tuple[float, list]] = {}
_CACHE_TTL_SECONDS = 900  # 15 min; NWS updates ~hourly

_NWS_USER_AGENT = "polymarket-arb-weather/1.0 (kaylaehman@pm.me)"


async def _nws_get(url: str, http) -> Optional[dict]:
    """GET a NWS URL with required User-Agent; returns JSON or None on any error."""
    try:
        resp = await http.get(url, headers={"User-Agent": _NWS_USER_AGENT})
        if resp.status_code != 200:
            logger.debug("[weather] NWS %s -> HTTP %d", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        logger.debug("[weather] NWS %s error: %s", url, exc)
        return None


async def _fetch_forecast_periods(lat: float, lon: float, http) -> Optional[list]:
    """Fetch NWS forecast periods list; returns None on any failure."""
    points_url = f"https://api.weather.gov/points/{lat},{lon}"
    points_data = await _nws_get(points_url, http)
    if not points_data:
        return None
    forecast_url = (points_data.get("properties") or {}).get("forecast")
    if not forecast_url:
        logger.debug("[weather] no forecast URL for %s,%s", lat, lon)
        return None

    now = _time.monotonic()
    cached = _forecast_cache.get(forecast_url)
    if cached is not None:
        fetched_at, periods = cached
        if now - fetched_at < _CACHE_TTL_SECONDS:
            return periods

    forecast_data = await _nws_get(forecast_url, http)
    if not forecast_data:
        return None
    periods = (forecast_data.get("properties") or {}).get("periods") or []
    _forecast_cache[forecast_url] = (now, periods)
    return periods


def _period_temp_for_date(periods: list, target_date: date) -> Optional[float]:
    """Return temperature (degF) of the isDaytime=True period on target_date."""
    target_str = target_date.isoformat()
    for period in periods:
        if not period.get("isDaytime", False):
            continue
        if period.get("startTime", "")[:10] == target_str:
            temp = period.get("temperature")
            if temp is not None:
                return float(temp)
    return None


async def forecast_high(
    series: str,
    target_date: date,
    *,
    http,
) -> Optional[float]:
    """Return NWS forecast high (degF) for a confirmed series on target_date.

    Returns None (never raises) when:
    - series is not in SERIES_STATION (unconfirmed station).
    - target_date is beyond the NWS ~7-day forecast horizon.
    - Any HTTP error occurs (network, NWS outage, rate-limit, etc.).
    - No daytime period for target_date exists in the forecast.
    """
    station = SERIES_STATION.get(series)
    if station is None:
        logger.debug("[weather] %s not in SERIES_STATION", series)
        return None

    try:
        periods = await _fetch_forecast_periods(station.lat, station.lon, http)
    except Exception as exc:
        logger.warning("[weather] unexpected fetch error for %s: %s", series, exc)
        return None

    if periods is None:
        return None
    return _period_temp_for_date(periods, target_date)


def forecast_margin(fc: float, threshold: float) -> float:
    """Return fc - threshold; negative means forecast is below the hot threshold."""
    return fc - threshold


def bucket_gate_keep(fc: float, lo: int, hi: int, safe_margin_f: float) -> bool:
    """Return True (KEEP the NO bet) when the forecast is comfortably outside the bucket.

    A NO bet on bucket [lo, hi] is safe only when the forecast is at least
    safe_margin_f degrees away from the nearest bucket edge on either side:
      KEEP:  fc <= lo - safe_margin_f  OR  fc >= hi + safe_margin_f
      SKIP:  lo - safe_margin_f < fc < hi + safe_margin_f  (forecast near/inside bucket)
    """
    return fc <= lo - safe_margin_f or fc >= hi + safe_margin_f
