"""Macro nowcast knowledge gate for the directional maker.

Mirrors core/market_data.py (financial gate): a pure ticker parser + gate math
plus a cached MacroNowcastClient that pulls free Federal Reserve nowcasts
(Cleveland Fed CPI/PCE, Atlanta Fed GDPNow via FRED). Gate keeps a NO longshot
only when the Kalshi threshold/bucket is >= min_sigma away from the nowcast.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Kalshi series prefix -> indicator key. Order matters for longest-prefix match:
# KXCPI is a prefix of KXCPICORE/KXCPIYOY, so check the specific ones first.
MACRO_SERIES: dict[str, str] = {
    "KXCPICORE": "CPICORE",
    "KXCPIYOY": "CPIYOY",
    "KXPCECORE": "PCECORE",
    "KXGDP": "GDP",
    "KXCPI": "CPI",
}

_SUFFIX_RE = re.compile(r"-([TB])(-?\d+\.?\d*)$")

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
# Cleveland Fed inflation nowcast (FusionCharts JSON). The month-over-month file
# carries the MoM CPI/Core-CPI/Core-PCE nowcast series used by the KXCPI/KXCPICORE/
# KXPCECORE markets. (KXCPIYOY is year-over-year — not in this MoM file; it maps to
# no series and safely returns None until YoY assembly is added — follow-up.)
_CLEVELAND_MONTH_URL = (
    "https://www.clevelandfed.org/-/media/files/webcharts/"
    "inflationnowcasting/nowcast_month.json?sc_lang=en"
)
_CLEVELAND_CPI_URL = _CLEVELAND_MONTH_URL
_CLEVELAND_PCE_URL = _CLEVELAND_MONTH_URL

# indicator -> Cleveland Fed FusionCharts seriesname (MoM file).
_CLEVELAND_SERIESNAME = {
    "CPI": "CPI Inflation",
    "CPICORE": "Core CPI Inflation",
    "PCECORE": "Core PCE Inflation",
}


@dataclass(frozen=True)
class MacroMarket:
    series: str
    indicator: str
    threshold: float
    direction: str  # "above"
    market_type: str  # "threshold" | "bucket"
    bucket_lo: Optional[float] = None
    bucket_hi: Optional[float] = None


def parse_macro_ticker(ticker: str) -> Optional[MacroMarket]:
    """Parse KXCPI*/KXCPIYOY*/KXCPICORE*/KXPCECORE*/KXGDP* tickers. None otherwise."""
    if not ticker:
        return None
    series = next((s for s in MACRO_SERIES if ticker.startswith(s + "-")), None)
    if series is None:
        return None
    m = _SUFFIX_RE.search(ticker)
    if m is None:
        return None
    tb, val_str = m.groups()
    try:
        threshold = float(val_str)
    except ValueError:
        return None
    indicator = MACRO_SERIES[series]
    if tb == "B":
        return MacroMarket(series, indicator, threshold, "above", "bucket", bucket_lo=threshold)
    return MacroMarket(series, indicator, threshold, "above", "threshold")


def macro_margin(nowcast: float, sigma: float, threshold: float) -> float:
    """z-score: how many σ the threshold sits above the nowcast.

    Positive z => threshold is ABOVE the nowcast (safe for NO on an 'above' market).
    Degenerate σ routes to SKIP (return -inf so z < any min_sigma).
    """
    if sigma <= 0:
        return float("-inf")
    return (threshold - nowcast) / sigma


def macro_threshold_keep(nowcast: float, sigma: float, threshold: float, min_sigma: float) -> bool:
    """KEEP a NO bet on an 'above-threshold' macro market when the threshold is
    >= min_sigma σ above the nowcast (i.e. the YES outcome is a deep tail)."""
    return macro_margin(nowcast, sigma, threshold) >= min_sigma


def macro_bucket_keep(nowcast: float, lo: float, hi: float, sigma: float, min_sigma: float) -> bool:
    """KEEP a NO bet on a bucket [lo,hi] when the nowcast is >= min_sigma σ OUTSIDE
    the bucket on either side (the bucket is a tail outcome). SKIP if σ<=0."""
    if sigma <= 0:
        return False
    margin = min_sigma * sigma
    return (nowcast <= lo - margin) or (nowcast >= hi + margin)


class MacroNowcastClient:
    """Fetches free Fed nowcasts with a TTL cache. Never raises (returns None)."""

    def __init__(self, http: Any, fred_api_key: Optional[str], ttl_s: int = 21600) -> None:
        self._http = http
        self._fred_key = fred_api_key
        self._ttl = ttl_s
        self._cache: dict[str, tuple[float, Optional[float]]] = {}

    async def nowcast(self, indicator: str) -> Optional[float]:
        now = time.monotonic()
        hit = self._cache.get(indicator)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]
        if indicator == "GDP":
            val = await self._fred("GDPNOW")
        elif indicator in ("CPI", "CPIYOY", "CPICORE"):
            val = await self._cleveland(_CLEVELAND_CPI_URL, indicator)
        elif indicator == "PCECORE":
            val = await self._cleveland(_CLEVELAND_PCE_URL, indicator)
        else:
            val = None
        self._cache[indicator] = (now, val)
        return val

    async def _fred(self, series_id: str) -> Optional[float]:
        if not self._fred_key:
            return None
        try:
            resp = await self._http.get(_FRED_BASE, params={
                "series_id": series_id, "api_key": self._fred_key,
                "file_type": "json", "sort_order": "desc", "limit": 1,
            })
            resp.raise_for_status()
            obs = resp.json().get("observations", [])
            return float(obs[0]["value"]) if obs else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[macro] FRED %s error: %s", series_id, exc)
            return None

    async def _cleveland(self, url: str, indicator: str) -> Optional[float]:
        if not url:
            return None
        try:
            resp = await self._http.get(url)
            resp.raise_for_status()
            return _parse_cleveland_nowcast(resp, indicator)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[macro] Cleveland %s error: %s", indicator, exc)
            return None


def _parse_cleveland_nowcast(resp: Any, indicator: str) -> Optional[float]:
    """Extract the latest nowcast for `indicator` from the Cleveland Fed MoM JSON.

    Format: a list whose first element has a ``dataset`` of FusionCharts series
    ``[{"seriesname": ..., "data": [{"value": "0.27", ...}, ...]}, ...]``. The
    nowcast is the last non-empty ``value`` of the matching series. Returns None
    for indicators with no MoM series (e.g. CPIYOY) or any shape mismatch.
    """
    name = _CLEVELAND_SERIESNAME.get(indicator)
    if name is None:
        return None
    try:
        data = resp.json()
        dataset = data[0]["dataset"]
    except (Exception,):  # noqa: BLE001
        return None
    for series in dataset:
        if series.get("seriesname") == name:
            vals = [d.get("value") for d in series.get("data", []) if d.get("value")]
            if not vals:
                return None
            try:
                return float(vals[-1])
            except (TypeError, ValueError):
                return None
    return None
