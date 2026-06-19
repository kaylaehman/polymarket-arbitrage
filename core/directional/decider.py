"""Decider — converts a DirectionalCandidate into a risk-gated DirectionalOrder.

Sizing rules (plan Task 11 REVISED):
- AI candidates (ai_probability is not None): fractional Kelly in YES-space.
  YES side: kelly_fraction(edge, yes_price=market_price, ai_probability, confidence, fraction)
  NO  side: kelly_fraction(edge, yes_price=1-market_price, ai_probability=1-ai_probability,
                           confidence, fraction)
  Notional = fraction * cash_balance, capped at min(max_position_usd, caps.max_position).
  Edge-based fallback (edge * kelly_frac * cash) when kelly returns 0 and edge > 0.
- Safe Compounder (ai_probability is None): fixed min(max_position_usd, caps.max_position).
- size = floor(notional / price), notional = size * price. Return None if size < 1.
- Risk gate via check_directional_order.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

from core.directional.models import DirectionalCandidate, DirectionalOrder
from core.kelly import kelly_fraction

logger = logging.getLogger(__name__)


class Decider:
    """Converts candidates into risk-gated DirectionalOrders.

    Args:
        risk_manager: Object with check_directional_order(...) -> bool.
        store: Object with directional_exposure() -> float and open_positions() -> list.
        kelly_frac: Fractional-Kelly multiplier (e.g. 0.25 = quarter-Kelly).
        max_position_usd: Hard cap per position in USD (also bounded by caps.max_position).
        cash_balance_fn: Callable returning current available cash balance.
        caps: Object with max_position, total_exposure, max_open attributes.
    """

    def __init__(
        self,
        risk_manager: Any,
        store: Any,
        kelly_frac: float,
        max_position_usd: float,
        cash_balance_fn: Callable[[], float],
        caps: Any,
    ) -> None:
        self._rm = risk_manager
        self._store = store
        self._kelly_frac = kelly_frac
        self._max_position_usd = max_position_usd
        self._cash_balance_fn = cash_balance_fn
        self._caps = caps

    def decide(self, candidate: DirectionalCandidate) -> DirectionalOrder | None:
        """Size and risk-gate a candidate. Returns an order or None."""
        notional = self._size_notional(candidate)
        if notional <= 0:
            return None

        price = candidate.market_price
        size = math.floor(notional / price)
        if size < 1:
            return None
        notional = size * price

        order = DirectionalOrder(
            market_id=candidate.market_id,
            side=candidate.side,
            price=price,
            size=size,
            notional=notional,
            strategy=candidate.strategy,
            reasoning=candidate.reasoning,
        )

        passed = self._rm.check_directional_order(
            order,
            open_count=len(self._store.open_positions()),
            directional_exposure=self._store.directional_exposure(),
            max_position=self._caps.max_position,
            max_total=self._caps.total_exposure,
            max_open=self._caps.max_open,
        )
        if not passed:
            logger.debug("Decider: risk gate rejected order for %s", candidate.market_id)
            return None

        return order

    def _size_notional(self, candidate: DirectionalCandidate) -> float:
        """Return desired notional (USD), before floor-size adjustment."""
        pos_cap = min(self._max_position_usd, self._caps.max_position)

        if candidate.ai_probability is None:
            # Safe Compounder: fixed size
            return pos_cap

        # AI-directional: fractional Kelly in YES-space
        cash = self._cash_balance_fn()
        if candidate.side == "YES":
            frac = kelly_fraction(
                edge=candidate.edge,
                yes_price=candidate.market_price,
                ai_probability=candidate.ai_probability,
                confidence=candidate.confidence,
                fraction=self._kelly_frac,
            )
        else:
            frac = kelly_fraction(
                edge=candidate.edge,
                yes_price=1.0 - candidate.market_price,
                ai_probability=1.0 - candidate.ai_probability,
                confidence=candidate.confidence,
                fraction=self._kelly_frac,
            )

        notional = frac * cash
        # Edge-based fallback: when Kelly returns 0 for a candidate that passed
        # edge gates, size using edge strength directly (edge * kelly_frac * cash).
        if notional <= 0 and candidate.edge > 0:
            notional = candidate.edge * self._kelly_frac * cash

        return min(notional, pos_cap)
