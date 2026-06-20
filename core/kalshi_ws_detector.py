"""
Event-driven Kalshi bundle-arb detector (Task 4) and detection-mode selector (Task 5).

WSBundleDetector:
  Receives live OrderBook updates from KalshiWSClient, runs the shared ArbEngine,
  and submits any signals to the ExecutionEngine with per-ticker cooldown dedup.

  DEDUP NOTE: the local cooldown_s dedup is intra-detector only.  Cross-path dedup
  (WS vs REST sweep) relies on the shared arb_engine._opportunity_cooldown (2 s per
  {market_id}_{opportunity_type} in core/arb_engine.py).  WSBundleDetector MUST be
  constructed with the bot's single self.kalshi_arb_engine instance — never a new
  ArbEngine — so both paths share the same opportunity cooldown dict.

decide_detection_mode:
  Pure function (no I/O) that chooses between the WS and REST paths for a single
  polling cycle.  Called by the _run_kalshi_trading supervisor each sweep.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from polymarket_client.models import Market, MarketState, OrderBook

logger = logging.getLogger(__name__)


class WSBundleDetector:
    """Async callback for KalshiWSClient that runs arb detection on each book update.

    Args:
        arb_engine: The bot's single shared ArbEngine instance.
        execution_engine: Engine with an async submit_signal(signal) method.
        market_titles: Mapping of Kalshi ticker -> question string (for MarketState).
        cooldown_s: Minimum seconds between signal submits for the same ticker.
        now_fn: Injectable clock (time.monotonic by default).
    """

    def __init__(
        self,
        arb_engine: Any,
        execution_engine: Any,
        market_titles: dict[str, str],
        cooldown_s: float = 5.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._arb = arb_engine
        self._exec = execution_engine
        self._titles = market_titles
        self._cooldown_s = cooldown_s
        self._now = now_fn
        self._last_submit: dict[str, float] = {}  # ticker -> last submit monotonic ts

    async def on_book_update(self, ticker: str, ob: OrderBook) -> None:
        """Receive a fresh OrderBook and run detection; submit signals when not cooling down."""
        market_state = MarketState(
            market=Market(
                market_id=f"kalshi:{ticker}",
                condition_id="",
                question=self._titles.get(ticker, ""),
            ),
            order_book=ob,
        )
        signals = self._arb.analyze(market_state)

        now = self._now()
        last = self._last_submit.get(ticker)
        if last is not None and now - last < self._cooldown_s:
            return

        for signal in signals:
            await self._exec.submit_signal(signal)

        if signals:
            self._last_submit[ticker] = now


# ── decide_detection_mode (Task 5) ────────────────────────────────────────────

def decide_detection_mode(
    ws_enabled: bool,
    ws_state: str,
    last_message_ts: Optional[float],
    now: float,
    staleness_s: float,
) -> tuple[str, str]:
    """Choose between WS-primary and REST-fallback for one polling cycle.

    Returns (mode, reason) where mode is 'ws' or 'rest' and reason is one of:
      'ws'                — WS is healthy, use it
      'rest:disabled'     — kalshi_ws_enabled flag is off
      'rest:disconnected' — WS not connected or no message received yet
      'rest:stale'        — last message is too old (age >= staleness_s)
    """
    if not ws_enabled:
        return ("rest", "rest:disabled")

    if ws_state != "connected" or last_message_ts is None:
        return ("rest", "rest:disconnected")

    if now - last_message_ts >= staleness_s:
        return ("rest", "rest:stale")

    return ("ws", "ws")
