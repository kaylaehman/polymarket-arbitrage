"""HighTempProvider: wraps the existing NWS daily-high forecaster and emits a
calibrated P(YES) by integrating a Normal(forecast, sigma) over the market's
strike interval. Validates the climate-provider framework end to end."""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any, Optional
from core.directional.climate.base import (
    ClimateProvider, ParsedClimate, ClimateSignal,
    interval_from_market, gaussian_interval_prob,
)
from core.weather import forecast_high  # existing NWS daily-high forecaster

_SIGMA_F = 3.5   # NWS next-day high-temp forecast error (°F); widen via calibration
# KXHIGH<CITY> and <CITY>HIGH / KX<CITY>HIGH variants seen in discovery
_TICKER = re.compile(r"^(KX)?([A-Z]+)?HIGH([A-Z]*)-(\d{2}[A-Z]{3}\d{2})-")


def _parse_date(yymmmdd: str) -> str:
    return datetime.strptime(yymmmdd, "%y%b%d").strftime("%Y-%m-%d")


class HighTempProvider(ClimateProvider):
    family = "high_temp"

    def match(self, market: Any) -> Optional[ParsedClimate]:
        ticker = getattr(market, "ticker", "")
        if "HIGH" not in ticker:
            return None
        m = _TICKER.match(ticker)
        if not m:
            return None
        series = ticker.split("-", 1)[0]
        try:
            date_iso = _parse_date(m.group(4))
        except ValueError:
            return None
        lo, hi = interval_from_market(getattr(market, "strike_type", None),
                                      getattr(market, "floor_strike", None),
                                      getattr(market, "cap_strike", None))
        return ParsedClimate("high_temp", "kalshi:" + ticker, series,
                             series, date_iso, getattr(market, "strike_type", "") or "",
                             lo, hi, "temp")

    async def probability(self, parsed: ParsedClimate, http: Any, ctx: dict) -> Optional[ClimateSignal]:
        try:
            d = datetime.strptime(parsed.target, "%Y-%m-%d").date()
            fc = await forecast_high(parsed.series, d, http=http)
            if fc is None:
                return None
            p = gaussian_interval_prob(parsed.lo, parsed.hi, mean=float(fc), sigma=_SIGMA_F)
            return ClimateSignal(p_yes=p, confidence=0.7, source="nws-high",
                                 drivers=[("forecast_high", float(fc)), ("sigma", _SIGMA_F)])
        except Exception:
            return None
