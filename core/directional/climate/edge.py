"""Turn a calibrated P(YES) + market price into 0..2 DirectionalCandidates:
longshot-NO (very-unlikely tail) and/or directional (model vs price divergence)."""
from __future__ import annotations
from typing import List
from core.directional.models import DirectionalCandidate
from core.directional.climate.base import ParsedClimate, ClimateSignal


def make_candidates(parsed: ParsedClimate, market_price: float, signal: ClimateSignal,
                    *, longshot_floor: float = 0.05, min_edge: float = 0.10,
                    strategy: str = "climate_paper") -> List[DirectionalCandidate]:
    p = signal.p_yes
    out: dict[str, DirectionalCandidate] = {}   # side -> candidate (dedups)

    def add(side: str, reasoning: str):
        if side in out:
            return
        out[side] = DirectionalCandidate(
            market_id=parsed.market_id, title=parsed.series, category="Climate and Weather",
            side=side, market_price=market_price, ai_probability=p,
            confidence=signal.confidence, edge=(p - market_price), strategy=strategy,
            reasoning=reasoning,
        )

    # Directional: model diverges from price by >= min_edge.
    if p - market_price >= min_edge:
        add("YES", f"model p={p:.2f} > price {market_price:.2f} ({signal.source})")
    elif market_price - p >= min_edge:
        add("NO", f"model p={p:.2f} < price {market_price:.2f} ({signal.source})")

    # Longshot-NO: YES is very unlikely.
    if p <= longshot_floor:
        add("NO", f"longshot: p(YES)={p:.3f} <= {longshot_floor}")

    return list(out.values())
