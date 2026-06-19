"""Safe Compounder strategy — pure-math NO-side directional.

Math adapted from /tmp/kalshi-ai-bot/src/strategies/safe_compounder.py (MIT).

KEY INSIGHT (from reference): YES last price is the primary certainty signal.
When YES trades near zero, the market is signalling near-certain NO resolution.
We compare a fair-value estimate against the actual NO ask to find edge.

Fair NO probability:
    fair_no ≈ 1 − yes_price

Edge (cents):
    edge_cents = (fair_no − no_ask) * 100

Trade logic (NO-side maker):
    - Emit a DirectionalCandidate at price = no_ask − 0.01 (resting maker order)
    - Only when edge_cents ≥ min_edge_cents
    - Skip categories in skip_categories
    - Skip markets where no_ask is unavailable
"""
from __future__ import annotations

from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from kalshi_client.models import KalshiMarket


class SafeCompounder(Strategy):
    """Pure-math NO-side strategy based on YES last-price edge.

    Args:
        min_edge_cents: Minimum edge in cents (e.g. 3 = $0.03) to emit a
            candidate.  Lower threshold = more trades but smaller edge per trade.
        skip_categories: Category strings to skip (e.g. ["Sports", "Entertainment"]
            for markets deemed too unpredictable for near-certain NO plays).
    """

    def __init__(self, min_edge_cents: float, skip_categories: list[str]) -> None:
        self._min_edge_cents = min_edge_cents
        self._skip = set(skip_categories)

    @property
    def name(self) -> str:
        return "safe_compounder"

    async def scan(
        self,
        markets: list[KalshiMarket],
        ctx: dict[str, Any],
    ) -> list[DirectionalCandidate]:
        """Scan markets for cheap NO opportunities.

        For each market:
        1. Skip excluded categories.
        2. Fetch no_ask via ctx["no_ask"](ticker); skip if unavailable.
        3. Compute fair_no = 1 − yes_price (last-price proxy).
        4. Compute edge_cents = (fair_no − no_ask) * 100.
        5. Emit a NO candidate at price = no_ask − 0.01 when edge ≥ threshold.
        """
        no_ask_fn = ctx["no_ask"]
        candidates: list[DirectionalCandidate] = []

        for market in markets:
            if market.category in self._skip:
                continue

            no_ask = no_ask_fn(market.ticker)
            if no_ask is None:
                continue

            fair_no = 1.0 - market.yes_price
            # Round to 10 dp to avoid floating-point artifacts (e.g. 0.96−0.93=0.02999…)
            edge_cents = round((fair_no - no_ask) * 100, 10)

            if edge_cents < self._min_edge_cents:
                continue

            candidates.append(
                DirectionalCandidate(
                    market_id=market.to_unified_market_id(),
                    title=market.title,
                    category=market.category,
                    side="NO",
                    market_price=no_ask - 0.01,
                    ai_probability=None,
                    confidence=None,
                    edge=edge_cents / 100.0,
                    strategy=self.name,
                    reasoning=(
                        f"fair_no={fair_no:.3f} no_ask={no_ask:.3f} "
                        f"edge={edge_cents:.1f}¢"
                    ),
                )
            )

        return candidates
