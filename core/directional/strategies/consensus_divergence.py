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
        return []  # gate logic added in later tasks
