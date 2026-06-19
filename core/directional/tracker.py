"""
Directional Tracker
===================

Monitors open directional positions and triggers exits.

Pure function
-------------
``should_exit(position, price, now, max_hold_hours) -> tuple[bool, str]``

  Operates in the position's own token-price space (YES price for YES
  positions, NO price for NO positions — callers must pass the correct
  side's mid-price).

  Exit reasons (checked in priority order):
    * ``"stop_loss"``   — price <= position.stop_loss
    * ``"take_profit"`` — price >= position.take_profit
    * ``"max_hold"``    — age (hours) > max_hold_hours
    * ``""``            — hold

Tracker class
-------------
``Tracker(store, kalshi_client, executor, risk_manager)``

  ``async sweep(now: datetime)``:
    1. For AI-directional positions: fetch current price; call should_exit.
       On exit: paper → mark closed; live (kill switch NOT triggered) →
       place opposing order (executor) then mark closed.
    2. For ALL positions: if market resolved → settle at 1.0 (YES) / 0.0
       (NO), mark closed, record realized P&L.  Resolution settlement
       proceeds even when kill switch is triggered.

Kill-switch gate: when mode=="live" and risk_manager.state.kill_switch_triggered,
skip placing live closing orders; resolution settlement still allowed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from core.directional.models import DirectionalPosition

logger = logging.getLogger(__name__)

# Strategies that use should_exit (vs. pure resolution-based strategies).
_AI_STRATEGIES = {"ai_directional", "safe_compounder"}

# Default max_hold hours used by sweep() when not overridden.
_DEFAULT_MAX_HOLD_HOURS = 72.0


def should_exit(
    position: DirectionalPosition,
    price: float,
    now: datetime,
    max_hold_hours: float,
) -> tuple[bool, str]:
    """Pure exit logic for a directional position.

    Args:
        position: The open position to evaluate.
        price: Current mid-price in the position's token-price space
               (YES price for YES positions, NO price for NO positions).
        now: Current UTC datetime.
        max_hold_hours: Maximum allowed holding period in hours.

    Returns:
        ``(True, reason)`` if the position should be closed, else ``(False, "")``.
    """
    if position.stop_loss is not None and price <= position.stop_loss:
        return (True, "stop_loss")

    if position.take_profit is not None and price >= position.take_profit:
        return (True, "take_profit")

    age_hours = (now - position.opened_at).total_seconds() / 3600.0
    if age_hours > max_hold_hours:
        return (True, "max_hold")

    return (False, "")


class Tracker:
    """Sweep open directional positions and apply exit rules."""

    def __init__(self, store, kalshi_client, executor, risk_manager) -> None:
        self._store = store
        self._client = kalshi_client
        self._executor = executor
        self._risk = risk_manager

    async def sweep(
        self,
        now: Optional[datetime] = None,
        max_hold_hours: float = _DEFAULT_MAX_HOLD_HOURS,
    ) -> None:
        """Check all open positions and close those that meet exit criteria."""
        if now is None:
            now = datetime.utcnow()

        positions = self._store.open_positions()

        for pos in positions:
            closed = await self._check_resolution(pos)
            if closed:
                continue

            if pos.strategy in _AI_STRATEGIES:
                await self._check_exit(pos, now, max_hold_hours)

    # ──────────────────────────────────────────────────────────────────────────

    async def _check_resolution(self, pos: DirectionalPosition) -> bool:
        """Settle position if the underlying market has resolved.

        Returns True if the position was closed by resolution.
        Resolution settlement is allowed regardless of kill-switch state.
        """
        try:
            market = await self._client.get_market(pos.market_id)
        except Exception as exc:
            logger.debug("get_market failed for %s: %s", pos.market_id, exc)
            return False

        if market is None or market.result is None:
            return False

        # Market resolved: 1.0 for the winning side, 0.0 for the losing side.
        if market.result.lower() == "yes":
            resolution_price = 1.0 if pos.side == "YES" else 0.0
        elif market.result.lower() == "no":
            resolution_price = 1.0 if pos.side == "NO" else 0.0
        else:
            return False

        realized_pnl = (resolution_price - pos.entry_price) * pos.size
        self._store.update_position(
            pos.market_id,
            status="closed",
            realized_pnl=realized_pnl,
            closed_at=datetime.utcnow().isoformat(),
        )
        logger.info(
            "Resolved %s %s side=%s pnl=%.4f",
            pos.market_id,
            market.result,
            pos.side,
            realized_pnl,
        )
        return True

    async def _check_exit(
        self,
        pos: DirectionalPosition,
        now: datetime,
        max_hold_hours: float,
    ) -> None:
        """Apply stop-loss / take-profit / max-hold exit rules."""
        current_price = await self._get_current_price(pos)
        if current_price is None:
            return

        exit_flag, reason = should_exit(pos, current_price, now, max_hold_hours)
        if not exit_flag:
            return

        logger.info("Exit signal '%s' for %s @ %.4f", reason, pos.market_id, current_price)

        kill_switch_active = (
            pos.mode == "live"
            and getattr(getattr(self._risk, "state", None), "kill_switch_triggered", False)
        )

        if not kill_switch_active and pos.mode == "live":
            # Live close: executor places opposing order.
            from core.directional.models import DirectionalOrder

            close_order = DirectionalOrder(
                market_id=pos.market_id,
                side="NO" if pos.side == "YES" else "YES",
                price=current_price,
                size=pos.size,
                notional=current_price * pos.size,
                strategy=pos.strategy,
                reasoning=f"exit:{reason}",
            )
            await self._executor.place(close_order, mode="live")

        realized_pnl = (current_price - pos.entry_price) * pos.size
        self._store.update_position(
            pos.market_id,
            status="closed",
            realized_pnl=realized_pnl,
            closed_at=datetime.utcnow().isoformat(),
        )

    async def _get_current_price(self, pos: DirectionalPosition) -> Optional[float]:
        """Fetch the mid-price for the position's token side."""
        ticker = pos.market_id.split("kalshi:", 1)[-1]
        try:
            ob = await self._client.get_orderbook_unified(ticker)
        except Exception as exc:
            logger.debug("get_orderbook_unified failed for %s: %s", ticker, exc)
            return None

        if ob is None:
            return None

        token_ob = ob.yes if pos.side == "YES" else ob.no
        mid = token_ob.mid_price
        if mid is None:
            # Fall back to best bid as a conservative estimate.
            mid = token_ob.best_bid
        return mid
