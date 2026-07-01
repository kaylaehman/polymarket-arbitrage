"""Decider — converts a DirectionalCandidate into a risk-gated DirectionalOrder.

Sizing rules (plan Task 11 REVISED):
- AI candidates (ai_probability is not None): fractional Kelly in YES-space.
  YES side: kelly_fraction(edge, yes_price=market_price, ai_probability, confidence, fraction)
  NO  side: kelly_fraction(edge, yes_price=1-market_price, ai_probability=1-ai_probability,
                           confidence, fraction)
  Notional = fraction * cash_balance, capped at min(max_position_usd, caps.max_position).
  I2 FIX: when Kelly returns 0 (non-positive EV), return None — NO edge-based fallback.
- Safe Compounder (ai_probability is None): fixed min(max_position_usd, caps.max_position).
- size = floor(notional / price), notional = size * price. Return None if size < 1.
- Risk gate via check_directional_order.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Callable

from core.directional.models import DirectionalCandidate, DirectionalOrder
from core.directional.store import category_for_market_id
from core.kelly import kelly_fraction

logger = logging.getLogger(__name__)

# "Daily" bets (weather daily-highs, Kalshi KXHIGH* + PM.US tc-temp*) resolve
# every day and are the fast-cycling, validated edge.  They are NOT subject to
# the longshot count cap — only the total_exposure $ governs them.  Everything
# else (macro CPI/PCE, cable, etc.) is a slower multi-day/week "longshot" that
# would otherwise clog the book, so its OPEN COUNT is capped (caps.max_open_longshot).
_DAILY_CATEGORY = "weather"


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
        # Optional max-payout cap (OPT-IN, default off): a position's max payout is
        # size * $1; at a tiny price a fixed notional buys a huge contract count
        # ($7 / $0.015 = 454). The real guard against that is the per-strategy price
        # band (see climate edge.py) which refuses extreme-price bets at the source;
        # this cap is only a belt-and-suspenders backstop, enabled by setting
        # caps.max_payout_per_position. Default 0 = disabled (does not clamp normal
        # sizing — an aggressive default clamped legitimate mid-price bets).
        max_payout = getattr(self._caps, "max_payout_per_position", 0.0)
        # isinstance guard: caps may be a MagicMock in tests (getattr returns a Mock,
        # not the default) — only clamp when a real positive number is configured.
        if isinstance(max_payout, (int, float)) and max_payout > 0:
            size = min(size, int(max_payout))
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

        # Per-bucket count cap: cap the number of open LONGSHOT (non-daily) bets
        # so slow multi-week positions (e.g. CPI) don't crowd out fast daily ones
        # (weather).  Daily bets are exempt — only total_exposure $ limits them.
        max_longshot = getattr(self._caps, "max_open_longshot", None)
        is_daily = category_for_market_id(candidate.market_id) == _DAILY_CATEGORY
        if not is_daily and max_longshot is not None and max_longshot >= 0:
            open_longshot = sum(
                1 for p in self._store.open_positions()
                if category_for_market_id(p.market_id) != _DAILY_CATEGORY
            )
            if open_longshot >= max_longshot:
                logger.debug(
                    "Decider: longshot bucket full (%d/%d) — rejecting %s",
                    open_longshot, max_longshot, candidate.market_id,
                )
                return None

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
            # NO bet: market_price is already the NO entry cost (1 - yes_price),
            # so the Kelly "price" is market_price itself; the win prob is P(NO).
            frac = kelly_fraction(
                edge=candidate.edge,
                yes_price=candidate.market_price,
                ai_probability=1.0 - candidate.ai_probability,
                confidence=candidate.confidence,
                fraction=self._kelly_frac,
            )

        notional = frac * cash
        # I2 FIX: do NOT fall back to edge-based sizing when Kelly is 0.
        # Kelly returning 0 means non-positive EV; sizing such bets corrupts P&L.
        return min(notional, pos_cap)
