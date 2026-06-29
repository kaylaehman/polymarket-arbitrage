"""ConsensusDivergence — paper directional bets on ~50/50 markets where an
independent knowledge gate (sports book consensus, macro nowcast) diverges from
the market's implied probability. Complements MakerLongshot (which only fires on
longshot-NO markets, ~weather-only). PAPER only — emits DirectionalCandidate.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from core.macro_data import parse_macro_ticker
from core.sports_data import kalshi_series_to_odds, match_team, kalshi_game_series_to_odds

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
        n_inrange = 0      # markets with a usable yes_mid in [min_yes, max_yes]
        n_gate_data = 0    # markets where a gate (sports/macro) returned a probability
        for m in markets:
            cat = getattr(m, "category", "")
            if cat in self._skip:
                continue
            yes_mid = float(getattr(m, "yes_price", 0) or 0)
            if not (self._min_yes <= yes_mid <= self._max_yes):
                continue
            n_inrange += 1
            sports_probs = None
            if sports is not None:
                if kalshi_series_to_odds(m.ticker):
                    try:
                        sports_probs = await sports.championship_probs(m.ticker)
                    except Exception:
                        sports_probs = None
                elif kalshi_game_series_to_odds(m.ticker):
                    try:
                        sports_probs = await sports.game_probs(m.ticker)
                    except Exception:
                        sports_probs = None
            if sports_probs:
                p_gate = match_team(getattr(m, "yes_sub_title", "") or "", sports_probs)
                if p_gate is not None:
                    n_gate_data += 1
                    res = divergence_side(p_gate, yes_mid, self._min_div)
                    if res is not None:
                        side, edge = res
                        market_price = yes_mid if side == "YES" else round(1 - yes_mid, 4)
                        candidates.append(DirectionalCandidate(
                            market_id=m.to_unified_market_id(), title=getattr(m, "title", ""),
                            category=cat, side=side, market_price=market_price,
                            ai_probability=p_gate, confidence=None, edge=edge, strategy=self.name,
                            reasoning=f"sports consensus {p_gate:.3f} vs mkt {yes_mid:.3f} -> {side}",
                        ))
            macro_client = ctx.get("macro")
            mm = parse_macro_ticker(m.ticker)
            if mm is not None and macro_client is not None and self._macro_cfg is not None:
                if mm.market_type == "bucket":
                    continue  # two-sided bucket prob is out of scope for this strategy
                try:
                    sigma = float(getattr(self._macro_cfg, "sigma", {}).get(mm.indicator, 0.0))
                    if sigma <= 0:
                        continue
                    nowcast = await macro_client.nowcast(mm.indicator)
                    if nowcast is None:
                        continue
                    n_gate_data += 1
                    z = (nowcast - mm.threshold) / (sigma * (2 ** 0.5))
                    p_gate = 0.5 * (1 + math.erf(z))
                    if mm.direction == "below":
                        p_gate = 1 - p_gate
                    res = divergence_side(p_gate, yes_mid, self._min_div)
                    if res is None:
                        continue
                    side, edge = res
                    market_price = yes_mid if side == "YES" else round(1 - yes_mid, 4)
                    candidates.append(DirectionalCandidate(
                        market_id=m.to_unified_market_id(), title=getattr(m, "title", ""),
                        category=cat, side=side, market_price=market_price,
                        ai_probability=p_gate, confidence=None, edge=edge, strategy=self.name,
                        reasoning=f"macro nowcast {nowcast:.3f} vs thr {mm.threshold:.3f} -> P(YES)={p_gate:.3f} vs mkt {yes_mid:.3f}",
                    ))
                except Exception:
                    continue
        logger.info(
            "[consensus_divergence] funnel: %d markets, %d in yes-range, %d gate-data, %d candidates",
            len(markets), n_inrange, n_gate_data, len(candidates),
        )
        return candidates
