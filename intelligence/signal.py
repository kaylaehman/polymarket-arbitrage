"""
Signal Dataclasses
==================

Pure-Python dataclasses for the intelligence layer. No external dependencies.

``MarketSignal``  — Claude's assessment of a single market.
``SignalSummary`` — the decision object the arb engine consumes.
``classify_direction`` — maps (ai_prob, market_price, confidence) -> direction.
"""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class MarketSignal:
    """Claude's assessment of whether a market's odds reflect recent news."""

    market_id: str
    market_question: str
    current_yes_price: float  # Current market price for YES (0.0-1.0)
    ai_probability: float     # Claude's estimated true probability (0.0-1.0)
    confidence: float         # Claude's confidence in its estimate (0.0-1.0)
    direction: str            # "agree" | "bullish" | "bearish" | "uncertain"
    reasoning: str            # Short explanation from Claude (1-2 sentences)
    news_headlines: list[str]  # Headlines that informed the analysis
    timestamp: datetime = field(default_factory=datetime.utcnow)
    cache_hit: bool = False   # Was this served from cache?

    @property
    def edge_vs_market(self) -> float:
        """Signed gap between Claude's probability and the market price.

        Positive => Claude thinks YES is underpriced (bullish).
        Negative => Claude thinks YES is overpriced (bearish).
        """
        return self.ai_probability - self.current_yes_price


@dataclass
class SignalSummary:
    """Aggregated signal used by the arb engine to filter or boost an arb."""

    signal: MarketSignal | None
    should_filter: bool       # True = skip this arb opportunity
    should_boost: bool        # True = consider a directional position
    adjusted_edge: float      # Original arb edge +/- signal adjustment
    reason: str               # Human-readable explanation

    @classmethod
    def neutral(cls, arb_edge: float, reason: str = "Intelligence unavailable") -> "SignalSummary":
        """A no-op summary: don't filter, don't boost, edge unchanged.

        Used whenever the intelligence layer is disabled, times out, or errors —
        the arb engine must proceed exactly as it would without intelligence.
        """
        return cls(
            signal=None,
            should_filter=False,
            should_boost=False,
            adjusted_edge=arb_edge,
            reason=reason,
        )


def classify_direction(ai_prob: float, market_price: float, confidence: float) -> str:
    """Classify how Claude's probability relates to the market price.

    Returns one of: "uncertain" | "bullish" | "bearish" | "agree".

    Thresholds (from INTELLIGENCE.md):
    - confidence < 0.5            -> "uncertain"  (don't trust the estimate)
    - delta > 0.05               -> "bullish"    (AI thinks YES underpriced)
    - delta < -0.05              -> "bearish"    (AI thinks YES overpriced)
    - otherwise                  -> "agree"      (within noise of the market)
    """
    delta = ai_prob - market_price
    if confidence < 0.5:
        return "uncertain"
    if delta > 0.05:
        return "bullish"
    if delta < -0.05:
        return "bearish"
    return "agree"
