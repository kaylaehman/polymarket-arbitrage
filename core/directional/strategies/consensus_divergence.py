"""ConsensusDivergence — paper directional bets on ~50/50 markets where an
independent knowledge gate (sports book consensus, macro nowcast) diverges from
the market's implied probability. Complements MakerLongshot (which only fires on
longshot-NO markets, ~weather-only). PAPER only — emits DirectionalCandidate.
"""
from __future__ import annotations

import logging
from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from core.sports_data import kalshi_series_to_odds, match_team

logger = logging.getLogger(__name__)


def divergence_side(p_gate: float, p_mkt: float, min_divergence: float):
    """Return ("YES"|"NO", edge) if |p_gate - p_mkt| >= min_divergence, else None."""
    diff = p_gate - p_mkt
    if abs(diff) < min_divergence:
        return None
    return ("YES", diff) if diff > 0 else ("NO", -diff)


class ConsensusDivergenceStrategy(Strategy):
    def __init__(self, *, min_divergence: float, max_yes_price: float = 0.95,
                 min_yes_price: float = 0.05, skip_categories: list,
                 sports_cfg: Any = None, macro_cfg: Any = None) -> None:
        self._min_div = min_divergence
        self._max_yes = max_yes_price
        self._min_yes = min_yes_price
        self._skip = set(skip_categories or [])
        self._sports_cfg = sports_cfg
        self._macro_cfg = macro_cfg

    @property
    def name(self) -> str:
        return "consensus_divergence"

    async def scan(self, markets: list, ctx: dict) -> list:
        candidates = []
        sports = ctx.get("sports")
        for m in markets:
            cat = getattr(m, "category", "")
            if cat in self._skip:
                continue
            yes_mid = float(getattr(m, "yes_price", 0) or 0)
            if not (self._min_yes <= yes_mid <= self._max_yes):
                continue
            if kalshi_series_to_odds(m.ticker) and sports is not None:
                try:
                    probs = await sports.championship_probs(m.ticker)
                    p_gate = match_team(getattr(m, "yes_sub_title", "") or "", probs)
                    if p_gate is None:
                        continue
                    res = divergence_side(p_gate, yes_mid, self._min_div)
                    if res is None:
                        continue
                    side, edge = res
                    market_price = yes_mid if side == "YES" else round(1 - yes_mid, 4)
                    candidates.append(DirectionalCandidate(
                        market_id=m.to_unified_market_id(),
                        title=getattr(m, "title", ""),
                        category=cat,
                        side=side,
                        market_price=market_price,
                        ai_probability=p_gate,
                        confidence=None,
                        edge=edge,
                        strategy=self.name,
                        reasoning=f"sports consensus {p_gate:.3f} vs mkt {yes_mid:.3f} -> {side}",
                    ))
                except Exception:
                    continue
        return candidates
