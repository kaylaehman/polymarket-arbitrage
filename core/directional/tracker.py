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
``Tracker(store, kalshi_client, executor, risk_manager, pmus_client=None)``

  ``async sweep(now: datetime, max_hold_hours, order_ttl_minutes)``:
    1. For AI-directional positions ONLY: fetch current price; call should_exit.
       On exit: paper → mark closed; live (kill switch NOT triggered) →
       call executor.close_position(position, price, mode="live") then mark closed.
    2. For ALL open positions: if market resolved → settle at 1.0 (YES) / 0.0
       (NO), mark closed, record realized P&L.  Resolution settlement
       proceeds even when kill switch is triggered.
    3. For PENDING maker positions (maker_longshot, live only):
       - poll get_order(order_id); on FILLED → mark status="open"
       - if age > order_ttl_minutes and still pending → cancel_order + mark closed

Kill-switch gate: when mode=="live" and risk_manager.state.kill_switch_triggered,
skip placing live closing orders; resolution settlement still allowed.

Safe Compounder and Maker Longshot positions are NOT in _AI_STRATEGIES — they
are held to resolution only (no stop-loss/take-profit/max-hold sweeping).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.directional.models import DirectionalPosition
from core.kalshi_fees import fee_per_contract
from music_intel.sources.markets import gamma_resolution
from polymarket_client.models import OrderStatus

logger = logging.getLogger(__name__)


def settlement_pnl(entry_price: float, size: float, resolution_price: float) -> float:
    """Realized P&L for a held-to-resolution position, NET of the entry fee.

    Kalshi charges a trading fee on the opening trade (``fee_per_contract`` at the
    entry price); settlement itself is free.  Paper used to record this GROSS,
    which overstated EV vs. the fee-NET backtest the strategy was validated on —
    so the category-breakdown go-live gate was reading inflated numbers.  This
    nets it out conservatively (see ``core/kalshi_fees``).
    """
    gross = (resolution_price - entry_price) * size
    return gross - fee_per_contract(entry_price) * size

# C1 FIX: safe_compounder removed — SC positions are held to resolution only,
# not subject to stop-loss/take-profit/max-hold sweep.
_AI_STRATEGIES = {"ai_directional"}

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

    # Normalize both to naive UTC for duration arithmetic to handle mixed
    # aware/naive datetimes gracefully (e.g. legacy test helpers still use utcnow()).
    now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
    opened_naive = (
        position.opened_at.replace(tzinfo=None)
        if position.opened_at.tzinfo is not None
        else position.opened_at
    )
    age_hours = (now_naive - opened_naive).total_seconds() / 3600.0
    if age_hours > max_hold_hours:
        return (True, "max_hold")

    return (False, "")


class Tracker:
    """Sweep open directional positions and apply exit rules."""

    def __init__(self, store, kalshi_client, executor, risk_manager, pmus_client=None, gamma_http=None) -> None:
        self._store = store
        self._client = kalshi_client
        self._executor = executor
        self._risk = risk_manager
        self._pmus_client = pmus_client
        self._gamma_http = gamma_http

    async def sweep(
        self,
        now: Optional[datetime] = None,
        max_hold_hours: float = _DEFAULT_MAX_HOLD_HOURS,
        order_ttl_minutes: float = 60.0,
    ) -> None:
        """Check all open and pending positions and apply exit rules."""
        if now is None:
            now = datetime.now(timezone.utc)

        # Sweep open positions (resolution + AI exit logic)
        positions = self._store.open_positions()
        resolved = 0
        for pos in positions:
            closed = await self._check_resolution(pos)
            if closed:
                resolved += 1
                continue

            if pos.strategy in _AI_STRATEGIES:
                await self._check_exit(pos, now, max_hold_hours)

        # Sweep pending maker positions (poll fill status / TTL cancel)
        pending = self._store.pending_positions()
        for pos in pending:
            await self._check_pending_maker(pos, now, order_ttl_minutes)

        # Visibility: log every sweep so "no resolved alerts" can be distinguished
        # from "sweep not running". Settle alerts fire from _check_resolution above.
        logger.info(
            "[tracker] sweep: %d open + %d pending checked, %d resolved this cycle%s",
            len(positions), len(pending), resolved,
            " (settle alerts sent)" if resolved else "",
        )

    # ──────────────────────────────────────────────────────────────────────────

    def _alert_settled(self, pos: DirectionalPosition, realized_pnl: float) -> None:
        """Fire a settle notification (this bet's P/L + overall realized P/L).

        Skips multi_outcome legs (they don't fire a place alert, and a 6-leg lock
        would spam).  Fire-and-forget — never raises into the sweep.  Overall P&L
        is read AFTER update_position, so it includes this settlement.
        """
        if pos.strategy == "multi_outcome":
            return
        try:
            import asyncio
            from core import alerts
            if alerts._ALERTER is None:
                return
            total = self._store.pnl_summary().get("total_realized_pnl", 0.0)
            if realized_pnl > 0:
                emoji, verb = "✅", "WON"
            elif realized_pnl < 0:
                emoji, verb = "❌", "LOST"
            else:
                emoji, verb = "➖", "flat"
            coro = alerts.notify(
                "directional_settled",
                f"{emoji} Bet settled: {pos.side} {pos.market_id} ${realized_pnl:+.2f}",
                f"{verb} **${realized_pnl:+.2f}** on {pos.side} @ ${pos.entry_price:.2f} "
                f"(x{pos.size})\nOverall realized P&L: **${total:+.2f}**",
                severity="info",
                dedup_key=f"{pos.market_id}:settled",
            )
            try:
                asyncio.get_running_loop().create_task(coro)
            except RuntimeError:
                asyncio.run(coro)
        except Exception:
            pass

    def _record_calibration_if_climate(self, pos: DirectionalPosition, result: str) -> None:
        """Log (predicted p_yes, actual outcome) for climate_paper settlements.

        Reads the most recent placed signal's ``ai_probability`` for this market
        as the predicted probability. No-op if the strategy isn't climate_paper
        or no placed signal with a probability exists. Never raises — this must
        never block or break settlement.
        """
        if pos.strategy != "climate_paper":
            return
        try:
            outcome_yes = 1 if result.lower() == "yes" else 0
            row = self._store._conn.execute(
                "SELECT ai_probability FROM directional_signals "
                "WHERE market_id = ? AND placed = 1 ORDER BY id DESC LIMIT 1",
                (pos.market_id,),
            ).fetchone()
            if row is None or row["ai_probability"] is None:
                return
            predicted_p = row["ai_probability"]
            self._store.record_calibration(pos.market_id, pos.strategy, predicted_p, outcome_yes)
        except Exception as exc:  # noqa: BLE001
            logger.debug("calibration recording failed for %s: %s", pos.market_id, exc)

    async def _check_resolution(self, pos: DirectionalPosition) -> bool:
        """Settle position if the underlying market has resolved.

        Returns True if the position was closed by resolution.
        Resolution settlement is allowed regardless of kill-switch state.
        """
        mid = pos.market_id
        if mid.startswith("pmus:"):
            return await self._check_pmus_resolution(pos)

        if mid.startswith("pm:"):
            return await self._check_pm_resolution(pos)

        # Strip the venue prefix: market_id is "kalshi:<ticker>" but get_market
        # expects the bare ticker. Without this, resolution NEVER fires.
        ticker = mid.split(":", 1)[1] if mid.startswith("kalshi:") else mid
        try:
            market = await self._client.get_market(ticker)
        except Exception as exc:
            logger.debug("get_market failed for %s: %s", ticker, exc)
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

        # NET of the Kalshi entry fee so paper EV matches the fee-net backtest
        # (the go-live gate reads these numbers — see settlement_pnl).
        realized_pnl = settlement_pnl(pos.entry_price, pos.size, resolution_price)
        self._store.update_position(
            pos.market_id,
            status="closed",
            realized_pnl=realized_pnl,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._record_calibration_if_climate(pos, market.result)
        logger.info(
            "Resolved %s %s side=%s pnl=%.4f (net of fees)",
            pos.market_id,
            market.result,
            pos.side,
            realized_pnl,
        )
        self._alert_settled(pos, realized_pnl)
        return True

    async def _check_pmus_resolution(self, pos: DirectionalPosition) -> bool:
        """Settle a pmus: position if the PM.US market has resolved.

        Resolution mapping:
          - PM.US YES wins (bucket HIT)   → resolution_price = 0.0 for our NO bet.
          - PM.US NO wins  (bucket MISSED) → resolution_price = 1.0 for our NO bet.

        P&L formula (matching Kalshi convention):
          realized_pnl = (resolution_price - entry_price) * size

        Returns True if the position was closed by resolution, False otherwise.
        Never raises — errors return False so the sweep continues.
        """
        if self._pmus_client is None:
            return False

        slug = pos.market_id.split("pmus:", 1)[1] if pos.market_id.startswith("pmus:") else pos.market_id
        try:
            result = await self._pmus_client.get_market_result(slug)
        except Exception as exc:
            logger.debug("pmus get_market_result failed for %s: %s", slug, exc)
            return False

        if result is None:
            return False

        # "yes" → YES side won → bucket was HIT → our NO bet LOST.
        # "no"  → NO side won  → bucket MISSED  → our NO bet WON.
        if result == "yes":
            resolution_price = 1.0 if pos.side == "YES" else 0.0
        elif result == "no":
            resolution_price = 1.0 if pos.side == "NO" else 0.0
        else:
            return False

        # NET of the entry fee (conservative; PM.US fees are typically lower than
        # Kalshi's, so this understates rather than overstates PM.US EV).
        realized_pnl = settlement_pnl(pos.entry_price, pos.size, resolution_price)
        self._store.update_position(
            pos.market_id,
            status="closed",
            realized_pnl=realized_pnl,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._record_calibration_if_climate(pos, result)
        logger.info(
            "Resolved %s %s side=%s pnl=%.4f (net of fees)",
            pos.market_id,
            result,
            pos.side,
            realized_pnl,
        )
        self._alert_settled(pos, realized_pnl)
        return True

    async def _check_pm_resolution(self, pos: DirectionalPosition) -> bool:
        """Settle a pm: (Polymarket music) position via Gamma resolution.

        "yes"/"no" = which named binary outcome won. Never raises.
        """
        if self._gamma_http is None:
            return False
        try:
            result = await gamma_resolution(self._gamma_http, pos.market_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("gamma_resolution failed for %s: %s", pos.market_id, exc)
            return False
        if result == "yes":
            resolution_price = 1.0 if pos.side == "YES" else 0.0
        elif result == "no":
            resolution_price = 1.0 if pos.side == "NO" else 0.0
        else:
            return False
        realized_pnl = settlement_pnl(pos.entry_price, pos.size, resolution_price)
        self._store.update_position(
            pos.market_id, status="closed", realized_pnl=realized_pnl,
            closed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._record_calibration_if_climate(pos, result)
        logger.info(
            "Resolved %s %s side=%s pnl=%.4f (music)",
            pos.market_id, result, pos.side, realized_pnl,
        )
        self._alert_settled(pos, realized_pnl)
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
            # C2 FIX: close position by SELLing the SAME token at its own-space price.
            # Delegate to executor.close_position so the SELL side/token is encapsulated.
            await self._executor.close_position(pos, current_price, mode="live")

        # Early exit is a round trip: fee on the entry AND the exit trade.
        gross = (current_price - pos.entry_price) * pos.size
        fees = (fee_per_contract(pos.entry_price) + fee_per_contract(current_price)) * pos.size
        realized_pnl = gross - fees
        self._store.update_position(
            pos.market_id,
            status="closed",
            realized_pnl=realized_pnl,
            closed_at=datetime.now(timezone.utc).isoformat(),
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

    async def _check_pending_maker(
        self,
        pos: DirectionalPosition,
        now: datetime,
        order_ttl_minutes: float,
    ) -> None:
        """Poll fill status for a pending live maker order; advance or cancel it.

        - FILLED / PARTIALLY_FILLED (full) → mark status="open" (hold to resolution).
        - Age > order_ttl_minutes and still unfilled → cancel_order + mark closed.
        - No order_id and mode="paper" → model the fill against the real orderbook
          via ``_check_paper_maker_fill`` (see below); never touches live order APIs.
        - No order_id and mode!="paper" → skip.
        """
        if pos.order_id is None:
            if getattr(pos, "mode", None) == "paper":
                await self._check_paper_maker_fill(pos, now, order_ttl_minutes)
            return

        try:
            result = await self._client.get_order(pos.order_id)
        except Exception as exc:
            logger.debug("get_order failed for %s: %s", pos.order_id, exc)
            return

        status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)

        # On fill: promote to open (will then be resolved via _check_resolution)
        if status in (OrderStatus.FILLED,):
            self._store.update_position(pos.market_id, status="open")
            logger.info("Maker order filled: %s → open", pos.market_id)
            return

        # Check TTL
        now_naive = now.replace(tzinfo=None) if now.tzinfo is not None else now
        opened_naive = (
            pos.opened_at.replace(tzinfo=None)
            if pos.opened_at.tzinfo is not None
            else pos.opened_at
        )
        age_minutes = (now_naive - opened_naive).total_seconds() / 60.0

        if age_minutes > order_ttl_minutes:
            try:
                await self._client.cancel_order(pos.order_id)
                logger.info("Maker TTL expired: cancelled %s", pos.order_id)
            except Exception as exc:
                logger.warning("cancel_order failed for %s: %s", pos.order_id, exc)
            self._store.update_position(
                pos.market_id,
                status="closed",
                realized_pnl=0.0,
                closed_at=now.isoformat(),
            )

    async def _check_paper_maker_fill(
        self,
        pos: DirectionalPosition,
        now: datetime,
        order_ttl_minutes: float,
    ) -> None:
        """Model a resting paper NO-buy against the real Kalshi orderbook.

        Fills iff the real NO ask reached <= post_price (pos.entry_price);
        otherwise the resting order is cancelled "unfilled" at TTL — this is
        NOT a trade, so it must be excluded from closed-position P&L/win-rate
        (those query status='closed'). Never raises into the sweep: if the
        orderbook fetch fails, leave the position pending and retry next cycle.
        """
        ticker = pos.market_id.split(":", 1)[-1]
        no_ask = None
        try:
            ob = await self._client.get_orderbook_unified(ticker)
            no_ask = getattr(getattr(ob, "no", None), "best_ask", None) if ob else None
        except Exception as exc:
            logger.debug("paper-fill orderbook fetch failed for %s: %s", ticker, exc)
            return  # leave pending, retry next cycle

        if no_ask is not None and no_ask <= pos.entry_price:
            self._store.update_position(pos.market_id, status="open")
            logger.info(
                "[paper-fill] %s filled (no_ask %.2f <= post %.2f)",
                pos.market_id, no_ask, pos.entry_price,
            )
            return

        # TTL: never filled -> not a trade
        now_naive = now.replace(tzinfo=None) if now.tzinfo else now
        op = pos.opened_at.replace(tzinfo=None) if pos.opened_at.tzinfo else pos.opened_at
        if (now_naive - op).total_seconds() / 60.0 > order_ttl_minutes:
            self._store.update_position(pos.market_id, status="unfilled", closed_at=now.isoformat())
            logger.info(
                "[paper-fill] %s unfilled at TTL (no_ask never <= post %.2f)",
                pos.market_id, pos.entry_price,
            )
