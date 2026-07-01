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
    yes = market_price            # market YES price — drives the side decision + edge
    out: dict[str, DirectionalCandidate] = {}   # side -> candidate (dedups)

    def add(side: str, reasoning: str):
        if side in out:
            return
        # Store the ENTRY COST of *this side*: downstream (decider sizing, Kelly,
        # executor booking) treats candidate.market_price as the cost of `side`.
        # A NO contract costs (1 - yes_price); see artist_paper._make_candidate.
        entry = yes if side == "YES" else round(1.0 - yes, 4)
        out[side] = DirectionalCandidate(
            market_id=parsed.market_id, title=parsed.series, category="Climate and Weather",
            side=side, market_price=entry, ai_probability=p,
            confidence=signal.confidence, edge=abs(p - yes), strategy=strategy,
            reasoning=reasoning,
        )

    # Trade ONLY inside a sane price band. At extreme prices (yes < 0.05 or > 0.95)
    # two bad things happen: (1) a large model-vs-market divergence there is almost
    # always model error (a liquid market pricing an outcome at 1.5% is usually right,
    # and a model claiming a 70-point edge is overconfident), and (2) sizing explodes
    # because the decider buys notional/price contracts — a NO at a 2¢ cost is
    # hundreds of contracts. Gate BOTH the directional and longshot-NO paths on the
    # band so no climate bet is ever placed at a pathological price.
    if not (0.05 <= yes <= 0.95):
        return []

    # Directional: model diverges from the market YES price by >= min_edge.
    if p - yes >= min_edge:
        add("YES", f"model p={p:.2f} > price {yes:.2f} ({signal.source})")
    elif yes - p >= min_edge:
        add("NO", f"model p={p:.2f} < price {yes:.2f} ({signal.source})")

    # Longshot-NO: YES is very unlikely (still inside the price band).
    if p <= longshot_floor:
        add("NO", f"longshot: p(YES)={p:.3f} <= {longshot_floor}")

    return list(out.values())
