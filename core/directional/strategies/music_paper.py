"""Adapter: run the alert-only music_intel engine and convert its ChartSignals
into PAPER DirectionalCandidates (category="music"). NEVER trades — refuses to
emit if the music engine reports execution enabled."""
from __future__ import annotations

import logging
from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy

logger = logging.getLogger(__name__)


class MusicPaperStrategy(Strategy):
    def __init__(self, *, engine, charts: list) -> None:
        self._engine = engine
        self._charts = list(charts)

    @property
    def name(self) -> str:
        return "music_paper"

    async def scan(self, markets: list, ctx: dict) -> list:
        if self._engine.execution_enabled():
            logger.warning("[music_paper] engine.execution_enabled() True -> emitting nothing")
            return []
        out = []
        for chart in self._charts:
            try:
                res = await self._engine.run_once(chart)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[music_paper] run_once(%s) failed: %s", chart, exc)
                continue
            for sig in getattr(res, "signals", []) or []:
                yes_p = float(getattr(sig, "market_prob", 0.0) or 0.0)
                market_price = yes_p if sig.side == "YES" else round(1 - yes_p, 4)
                out.append(DirectionalCandidate(
                    market_id=sig.market_id,
                    title=getattr(sig, "question", "") or getattr(sig, "target", ""),
                    category="music",
                    side=sig.side,
                    market_price=market_price,
                    ai_probability=getattr(sig, "model_prob", None),
                    confidence=getattr(sig, "confidence", None),
                    edge=getattr(sig, "net_edge", 0.0),
                    strategy=self.name,
                    reasoning=f"music edge {getattr(sig,'target','')}: model {getattr(sig,'model_prob',0):.3f} vs mkt {yes_p:.3f}",
                ))
        return out
