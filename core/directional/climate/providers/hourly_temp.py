"""HourlyTempProvider: wraps the NWS hourly forecaster and emits a calibrated
P(YES) by integrating a Normal(hourly_forecast, sigma) over the market's
strike interval. Handles Kalshi KXTEMP<CITY>H "Hourly Directional
Temperature" markets."""
from __future__ import annotations
import re
from datetime import datetime
from typing import Any, Optional
from core.directional.climate.base import (
    ClimateProvider, ParsedClimate, ClimateSignal,
    interval_from_market, gaussian_interval_prob,
)
from core.weather import forecast_hour

_SIGMA_F = 2.5   # hourly temp forecast error (°F); calibrate
# KXTEMP<CITY>H-YYMMMDDHH-...  (date is YYMMMDD, then 2-digit hour)
_TICKER = re.compile(r"^(KXTEMP[A-Z]+H)-(\d{2}[A-Z]{3}\d{2})(\d{2})-")


class HourlyTempProvider(ClimateProvider):
    family = "hourly_temp"

    def match(self, market: Any) -> Optional[ParsedClimate]:
        ticker = getattr(market, "ticker", "")
        m = _TICKER.match(ticker)
        if not m:
            return None
        try:
            date_iso = datetime.strptime(m.group(2), "%y%b%d").strftime("%Y-%m-%d")
        except ValueError:
            return None
        target = f"{date_iso}T{m.group(3)}"
        lo, hi = interval_from_market(getattr(market, "strike_type", None),
                                      getattr(market, "floor_strike", None),
                                      getattr(market, "cap_strike", None))
        return ParsedClimate("hourly_temp", "kalshi:" + ticker, m.group(1),
                             m.group(1), target, getattr(market, "strike_type", "") or "",
                             lo, hi, "temp")

    async def probability(self, parsed: ParsedClimate, http: Any, ctx: dict) -> Optional[ClimateSignal]:
        try:
            fc = await forecast_hour(parsed.series, parsed.target, http=http)
            if fc is None:
                return None
            p = gaussian_interval_prob(parsed.lo, parsed.hi, mean=float(fc), sigma=_SIGMA_F)
            return ClimateSignal(p_yes=p, confidence=0.7, source="nws-hourly",
                                 drivers=[("forecast_hour", float(fc)), ("sigma", _SIGMA_F)])
        except Exception:
            return None
