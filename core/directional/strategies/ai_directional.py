"""AI-directional strategy — Claude intelligence → YES/NO candidate.

For each market, calls intelligence_engine.evaluate(...) and emits a
DirectionalCandidate when confidence and edge gates are met.

Fail-safe: any exception in per-market processing is caught; that market
is skipped without propagating the error.
"""
from __future__ import annotations

import logging
from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from kalshi_client.models import KalshiMarket
from utils.edge_filter import passes_edge
from utils.structural_bias import structural_score

logger = logging.getLogger(__name__)


class AiDirectional(Strategy):
    """Strategy that converts intelligence engine signals into directional candidates.

    Args:
        intelligence_engine: Object with ``evaluate(**kwargs) -> SignalSummary|None``.
        min_confidence: Minimum signal confidence required (e.g. 0.60).
        min_edge_pct: Minimum |edge_vs_market| required (e.g. 0.05 = 5 cents).
    """

    def __init__(
        self,
        intelligence_engine: Any,
        min_confidence: float,
        min_edge_pct: float,
    ) -> None:
        self._intel = intelligence_engine
        self._min_confidence = min_confidence
        self._min_edge_pct = min_edge_pct

    @property
    def name(self) -> str:
        return "ai_directional"

    async def scan(
        self,
        markets: list[KalshiMarket],
        ctx: dict[str, Any],
    ) -> list[DirectionalCandidate]:
        """Evaluate each market and return candidates that pass all gates."""
        candidates: list[DirectionalCandidate] = []

        for m in markets:
            try:
                summary = await self._intel.evaluate(
                    market_id=m.ticker,
                    market_question=m.title,
                    current_yes_price=m.yes_price,
                    arb_edge=0.0,
                    resolution_criteria=None,
                )
                if summary is None or summary.signal is None:
                    continue

                signal = summary.signal
                conf = signal.confidence
                raw_edge = abs(signal.edge_vs_market)

                if conf < self._min_confidence:
                    continue
                if not passes_edge(conf, raw_edge):
                    continue
                if raw_edge < self._min_edge_pct:
                    continue

                side = "YES" if signal.direction == "bullish" else "NO"
                # I3 FIX: candidate.edge = raw AI edge only (vetted by the gates above).
                # structural_score is retained as a tiebreaker for future logging but
                # must NOT be folded into the sizing edge — doing so inflates Kelly
                # beyond what the confidence/edge gates vetted.
                _ = structural_score(m.yes_price, side, m.category)  # tiebreaker (unused)

                candidates.append(
                    DirectionalCandidate(
                        market_id=m.to_unified_market_id(),
                        title=m.title,
                        category=m.category,
                        side=side,
                        market_price=m.yes_price,
                        ai_probability=signal.ai_probability,
                        confidence=conf,
                        edge=raw_edge,
                        strategy=self.name,
                        reasoning=getattr(signal, "reasoning", ""),
                    )
                )
            except Exception:
                logger.exception("AiDirectional: error evaluating %s — skipping", m.ticker)

        return candidates
