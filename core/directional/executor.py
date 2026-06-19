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
"""

from __future__ import annotations

import logging
from datetime import datetime
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

    # ──────────────────────────────────────────────────────────────────────────

    def _record(
        self,
        order: DirectionalOrder,
        mode: str,
        stop_loss: Optional[float],
        take_profit: Optional[float],
    ) -> DirectionalPosition:
        pos = DirectionalPosition(
            market_id=order.market_id,
            side=order.side,
            entry_price=order.price,
            size=order.size,
            strategy=order.strategy,
            mode=mode,
            opened_at=datetime.utcnow(),
            stop_loss=stop_loss,
            take_profit=take_profit,
            notional=order.notional,
            status="open",
        )
        self._store.record_position(pos)
        return pos
