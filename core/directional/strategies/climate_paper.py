"""climate_paper strategy: for each liquid market, find a climate provider, get a
calibrated P(YES), and emit longshot-NO / directional candidates. PAPER only.
Never raises into the engine cycle."""
from __future__ import annotations
import logging
from typing import Any, List
from core.directional.strategies.base import Strategy
from core.directional.models import DirectionalCandidate
from core.directional.climate.edge import make_candidates

logger = logging.getLogger(__name__)


class ClimatePaperStrategy(Strategy):
    def __init__(self, registry, cfg):
        self._registry = registry
        self._cfg = cfg

    @property
    def name(self) -> str:
        return "climate_paper"

    async def scan(self, markets: List[Any], ctx: dict) -> List[DirectionalCandidate]:
        if not getattr(self._cfg, "enabled", False):
            return []
        http = ctx.get("http")
        out: List[DirectionalCandidate] = []
        for m in markets:
            try:
                hit = self._registry.match(m)
                if hit is None:
                    continue
                provider, parsed = hit
                signal = await provider.probability(parsed, http, ctx)
                if signal is None:
                    continue
                yes_price = float(getattr(m, "yes_price", 0.0) or 0.0)
                out.extend(make_candidates(
                    parsed, yes_price, signal,
                    longshot_floor=self._cfg.longshot_floor,
                    min_edge=self._cfg.min_edge,
                ))
            except Exception as exc:  # never break the cycle
                logger.warning("[climate_paper] %s error: %s", getattr(m, "ticker", "?"), exc)
        return out
