"""
Directional Executor
====================

Places directional orders in paper or live mode.

Paper mode: records a DirectionalPosition with mode="paper"; no API call.
Live mode:
  1. Pre-flight balance guard — aborts if balance < order.notional.
  2. Converts side string to enums (directional only buys).
  3. Calls kalshi_client.place_order(...).
  4. Records a DirectionalPosition with mode="live".

Maker (maker_longshot strategy):
  Paper: records position at post_price immediately (status="open", simulated fill).
  Live: places a resting (non-marketable) NO BUY limit; records PENDING position
        with the returned order_id (status="pending"). Tracker polls for fill/TTL.

Closing (C2 fix):
  close_position(position, price, mode) — SELLs the SAME token at own-space price.
  Paper: no API call (position marked closed by tracker).
  Live: calls place_order with SELL side and the position's own token type.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from polymarket_client.models import OrderSide, TokenType

from core.directional.models import DirectionalOrder, DirectionalPosition

logger = logging.getLogger(__name__)


class Executor:
    """Paper/live order placement for directional trading."""

    def __init__(self, kalshi_client, store) -> None:
        self._client = kalshi_client
        self._store = store

    async def place(
        self,
        order: DirectionalOrder,
        mode: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Optional[DirectionalPosition]:
        """Place a directional order and record the resulting position.

        Args:
            order: The order to place.
            mode: "paper" or "live".
            stop_loss: Optional stop-loss price in token-price space.
            take_profit: Optional take-profit price in token-price space.

        Returns:
            A DirectionalPosition on success, or None if aborted.
        """
        # Maker longshot: paper simulates immediate fill; live places resting order.
        if order.strategy == "maker_longshot":
            return await self._place_maker(order, mode, stop_loss, take_profit)

        if mode == "paper":
            return self._record(order, mode, stop_loss, take_profit)

        # Live path ────────────────────────────────────────────────────────────
        bal = await self._client.get_balance()
        if bal < order.notional:
            logger.warning(
                "Directional live order aborted: balance %.2f < notional %.2f for %s",
                bal,
                order.notional,
                order.market_id,
            )
            return None

        token_type = TokenType.YES if order.side == "YES" else TokenType.NO
        action_side = OrderSide.BUY  # directional trading only buys

        try:
            await self._client.place_order(
                ticker=order.market_id,
                token_type=token_type,
                side=action_side,
                price=order.price,
                size=order.size,
                strategy_tag=order.strategy,
            )
        except Exception as exc:
            logger.error("place_order failed for %s: %s", order.market_id, exc)
            return None

        return self._record(order, mode, stop_loss, take_profit)

    async def close_position(
        self,
        position: DirectionalPosition,
        price: float,
        mode: str,
    ) -> None:
        """C2 FIX: Close a position by SELLing the SAME token at own-space price.

        Args:
            position: The open position to close.
            price: Current mid-price in the position's own token-price space.
            mode: "paper" (no API call) or "live" (SELL via kalshi_client).
        """
        if mode != "live":
            # Paper close: tracker marks closed; no API call needed.
            return

        token_type = TokenType.YES if position.side == "YES" else TokenType.NO
        try:
            await self._client.place_order(
                ticker=position.market_id,
                token_type=token_type,
                side=OrderSide.SELL,  # SELL the held token to exit
                price=price,
                size=position.size,
                strategy_tag=position.strategy,
            )
        except Exception as exc:
            logger.error("close_position failed for %s: %s", position.market_id, exc)

    # ──────────────────────────────────────────────────────────────────────────

    async def _place_maker(
        self,
        order: DirectionalOrder,
        mode: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> Optional[DirectionalPosition]:
        """Handle maker_longshot order placement.

        Paper: record position as immediately open at post_price (simulated fill).
        Live:  balance-check, place resting NO BUY limit, record PENDING with order_id.
        """
        if mode == "paper":
            return self._record(order, mode, stop_loss, take_profit, status="open")

        # Live: pre-flight balance guard
        bal = await self._client.get_balance()
        if bal < order.notional:
            logger.warning(
                "Maker live order aborted: balance %.2f < notional %.2f for %s",
                bal,
                order.notional,
                order.market_id,
            )
            return None

        token_type = TokenType.NO  # maker_longshot is always NO BUY
        try:
            placed = await self._client.place_order(
                ticker=order.market_id,
                token_type=token_type,
                side=OrderSide.BUY,
                price=order.price,
                size=order.size,
                strategy_tag=order.strategy,
            )
        except Exception as exc:
            logger.error("maker place_order failed for %s: %s", order.market_id, exc)
            return None

        order_id = getattr(placed, "order_id", None)
        if not order_id:
            logger.error(
                "maker place_order returned no order_id for %s; "
                "not recording to avoid unmanaged pending position",
                order.market_id,
            )
            return None
        return self._record(order, mode, stop_loss, take_profit, status="pending", order_id=order_id)

    def _record(
        self,
        order: DirectionalOrder,
        mode: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        status: str = "open",
        order_id: Optional[str] = None,
    ) -> DirectionalPosition:
        pos = DirectionalPosition(
            market_id=order.market_id,
            side=order.side,
            entry_price=order.price,
            size=order.size,
            strategy=order.strategy,
            mode=mode,
            opened_at=datetime.now(timezone.utc),
            stop_loss=stop_loss,
            take_profit=take_profit,
            notional=order.notional,
            status=status,
            order_id=order_id,
        )
        self._store.record_position(pos)
        # Fire-and-forget alert (gated; never raises into the caller)
        try:
            import asyncio
            from core import alerts
            if alerts._ALERTER is not None:
                coro = alerts.notify(
                    "directional_open",
                    f"{order.strategy} {order.side} {order.market_id}",
                    f"price={order.price} size={order.size} mode={mode}",
                    severity="info",
                    dedup_key=order.market_id,
                )
                try:
                    asyncio.get_running_loop().create_task(coro)
                except RuntimeError:
                    asyncio.run(coro)
        except Exception:
            pass
        return pos

