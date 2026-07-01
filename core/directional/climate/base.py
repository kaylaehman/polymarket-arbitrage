"""Climate provider framework: parsed-market + signal types, the provider ABC,
and the forecast→probability math. Pure (no I/O) except provider.probability()."""
from __future__ import annotations
import abc, math
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ParsedClimate:
    family: str            # "high_temp" | "hourly_temp" | ...
    market_id: str         # "kalshi:<ticker>"
    series: str            # e.g. "KXTEMPNYCH"
    geo: str               # station/series key for the forecast source (e.g. "nyc")
    target: str            # ISO date or "YYYY-MM-DDTHH" for hourly / "YYYY-MM" monthly
    strike_type: str       # "greater" | "less" | "between"
    lo: Optional[float]    # interval lower bound (None = open)
    hi: Optional[float]    # interval upper bound (None = open)
    kind: str              # "temp" | "count" | "precip"


@dataclass
class ClimateSignal:
    p_yes: float
    confidence: float
    source: str
    drivers: list = field(default_factory=list)


def interval_from_market(strike_type: Optional[str], floor: Optional[float],
                         cap: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Map Kalshi (strike_type, floor_strike, cap_strike) -> (lo, hi) for the YES region."""
    st = (strike_type or "").lower()
    if st == "greater":
        return (floor, None)
    if st == "less":
        return (None, cap)
    if st == "between":
        return (floor, cap)
    return (floor, cap)  # best-effort fallback


def _norm_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def gaussian_interval_prob(lo: Optional[float], hi: Optional[float],
                           mean: float, sigma: float) -> float:
    """P(lo < X <= hi) for X ~ Normal(mean, sigma). None bound = open on that side."""
    p_hi = _norm_cdf(hi, mean, sigma) if hi is not None else 1.0
    p_lo = _norm_cdf(lo, mean, sigma) if lo is not None else 0.0
    return max(0.0, min(1.0, p_hi - p_lo))


class ClimateProvider(abc.ABC):
    family: str = "climate"

    @abc.abstractmethod
    def match(self, market: Any) -> Optional[ParsedClimate]:
        """Return ParsedClimate if this provider handles the market, else None."""

    @abc.abstractmethod
    async def probability(self, parsed: ParsedClimate, http: Any,
                          ctx: dict) -> Optional[ClimateSignal]:
        """Return calibrated P(YES) signal, or None to skip. Must never raise."""
