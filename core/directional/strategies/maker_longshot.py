"""Maker Longshot strategy — NO-bias resting limit on longshot Kalshi markets.

EDGE: on longshot markets (YES mid <= max_yes_price, so NO >= ~0.85), the
structural longshot/NO bias (Jon-Becker/pma research) makes NO underpriced.
Acting as MAKER (resting NO BUY limit at post_price, 0% fee) captures the
spread + the bias. Hold to resolution.

Resting maker price:
    post_price = round(no_ask - price_improvement_cents/100.0, 2)
    clamped to [0.01, 0.99]; must be strictly below no_ask to be non-marketable.
"""
from __future__ import annotations

from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy
from kalshi_client.models import KalshiMarket
from utils.structural_bias import structural_score


class MakerLongshotStrategy(Strategy):
    """Post resting NO BUY limits on structurally-favoured longshot markets.

    Args:
        min_structural_score: Minimum structural_score(yes_mid, "NO", category).
        max_yes_price: Skip markets where yes_mid > this (longshot filter).
        price_improvement_cents: How many cents below no_ask to post the bid.
        skip_categories: Category strings to skip entirely.
    """

    def __init__(
        self,
        min_structural_score: float,
        max_yes_price: float,
        price_improvement_cents: int,
        skip_categories: list[str],
    ) -> None:
        self._min_score = min_structural_score
        self._max_yes = max_yes_price
        self._pip = price_improvement_cents
        self._skip = set(skip_categories)

    @property
    def name(self) -> str:
        return "maker_longshot"

    async def scan(
        self,
        markets: list[KalshiMarket],
        ctx: dict[str, Any],
    ) -> list[DirectionalCandidate]:
        """Scan for longshot NO maker opportunities.

        For each market:
        1. Skip excluded categories.
        2. Skip if yes_mid <= 0 or > max_yes_price (not a longshot).
        3. Compute structural_score(yes_mid, "NO", category); skip if < min.
        4. Fetch no_ask; skip if unavailable.
        5. Build resting post_price strictly below no_ask.
        6. Emit NO DirectionalCandidate with strategy="maker_longshot".
        """
        no_ask_fn = ctx["no_ask"]
        candidates: list[DirectionalCandidate] = []

        for market in markets:
            if market.category in self._skip:
                continue

            yes_mid = market.yes_price
            if yes_mid <= 0 or yes_mid > self._max_yes:
                continue

            score = structural_score(yes_mid, "NO", market.category)
            if score < self._min_score:
                continue

            no_ask = no_ask_fn(market.ticker)
            if no_ask is None:
                continue

            # Build non-marketable resting bid: strictly < no_ask
            improvement = self._pip / 100.0
            post_price = round(no_ask - improvement, 2)
            post_price = max(0.01, min(0.99, post_price))
            if post_price >= no_ask:
                post_price = round(no_ask - 0.01, 2)
            if post_price <= 0:
                continue

            candidates.append(
                DirectionalCandidate(
                    market_id=market.to_unified_market_id(),
                    title=market.title,
                    category=market.category,
                    side="NO",
                    market_price=post_price,
                    ai_probability=None,
                    confidence=None,
                    edge=score,
                    strategy=self.name,
                    reasoning=(
                        f"yes_mid={yes_mid:.3f} score={score:.4f} "
                        f"no_ask={no_ask:.3f} post={post_price:.3f}"
                    ),
                )
            )

        return candidates
