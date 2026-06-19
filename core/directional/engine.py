"""Directional Engine — Task 14.

Wires scanner, strategies, decider, executor, and tracker into a single
async loop that runs at a configurable interval.

Factory pattern: __init__ builds all sub-components from config + shared
services (kalshi_client, intelligence_engine, risk_manager).

No changes to arb loop, ExecutionEngine, DataFeed, or cross-platform monitor.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from core.directional.decider import Decider
from core.directional.executor import Executor
from core.directional.scanner import KalshiMarketScanner
from core.directional.store import DirectionalStore
from core.directional.strategies.ai_directional import AiDirectional
from core.directional.strategies.safe_compounder import SafeCompounder
from core.directional.tracker import Tracker
from utils.kalshi_categories import categorize

logger = logging.getLogger(__name__)


class DirectionalEngine:
    """Top-level loop that orchestrates the directional trading cycle.

    Args:
        config: The ``config.directional`` config namespace.
        kalshi_client: Shared KalshiClient (read-only shared; arb loop unaffected).
        intelligence_engine: Shared IntelligenceEngine (may be None).
        risk_manager: Shared RiskManager (directional caps only).
    """

    def __init__(
        self,
        config: Any,
        kalshi_client: Any,
        intelligence_engine: Optional[Any],
        risk_manager: Any,
    ) -> None:
        self._cfg = config
        self._client = kalshi_client
        self._intel = intelligence_engine
        self._rm = risk_manager
        self._running = False

        # Build store
        self.store = DirectionalStore(config.db_path)
        self.store.init_schema()

        # Build scanner (M1: min_volume is a proper field on DirectionalConfig)
        self.scanner = KalshiMarketScanner(
            kalshi_client=kalshi_client,
            categorize_fn=categorize,
            min_volume=config.min_volume,
            exclude_categories=list(config.category_exclude),
        )

        # Build strategies
        self._strategies = []
        sc_cfg = getattr(config, "safe_compounder", None)
        if sc_cfg is not None:
            self._strategies.append(
                (SafeCompounder(
                    min_edge_cents=sc_cfg.min_edge_cents,
                    skip_categories=list(getattr(sc_cfg, "skip_categories", [])),
                ), sc_cfg)
            )

        ai_cfg = getattr(config, "ai_directional", None)
        if ai_cfg is not None and intelligence_engine is not None:
            self._strategies.append(
                (AiDirectional(
                    intelligence_engine=intelligence_engine,
                    min_confidence=ai_cfg.min_confidence,
                    min_edge_pct=ai_cfg.min_edge_pct,
                ), ai_cfg)
            )

        # Build decider (caps from config; cash balance = 100 placeholder without a live query)
        self.decider = Decider(
            risk_manager=risk_manager,
            store=self.store,
            kelly_frac=getattr(ai_cfg, "kelly_fraction", 0.25) if ai_cfg else 0.25,
            max_position_usd=config.caps.max_position,
            cash_balance_fn=self._get_cash_balance,
            caps=config.caps,
        )

        # Build executor
        self.executor = Executor(kalshi_client=kalshi_client, store=self.store)

        # Build tracker
        self.tracker = Tracker(
            store=self.store,
            kalshi_client=kalshi_client,
            executor=self.executor,
            risk_manager=risk_manager,
        )

        # Max hold hours for tracker (default 72)
        self._max_hold_hours = getattr(ai_cfg, "max_hold_hours", 72.0) if ai_cfg else 72.0

    def _get_cash_balance(self) -> float:
        """Synchronous placeholder returning a fixed balance for Kelly sizing."""
        # In paper mode this doesn't affect live money; live mode uses risk caps.
        return 100.0

    def _build_sc_ctx(self) -> dict:
        """Build SafeCompounder context with a no_ask closure over kalshi_client."""
        # no_ask is fetched from the orderbook synchronously using a stored cache.
        # The scan is async so we resolve orderbooks before calling scan().
        # Here we store a dict that scan() will look up.
        cache: dict[str, float | None] = {}

        def no_ask_fn(ticker: str) -> float | None:
            return cache.get(ticker)

        return {"no_ask": no_ask_fn, "_cache": cache}

    async def _populate_sc_ctx(self, markets: list, ctx: dict) -> None:
        """Async-populate the no_ask cache from real orderbooks."""
        cache = ctx["_cache"]
        for m in markets:
            try:
                ob = await self._client.get_orderbook_unified(m.ticker)
                if ob is not None and ob.no is not None:
                    ask = getattr(ob.no, "best_ask", None)
                    cache[m.ticker] = ask
                else:
                    cache[m.ticker] = None
            except Exception:
                cache[m.ticker] = None

    async def run_once(self) -> None:
        """Execute one full scan → decide → execute → sweep cycle."""
        markets = await self.scanner.scan(self._cfg.markets_per_cycle)

        # Build SafeCompounder context (needs async orderbook fetches)
        sc_ctx = self._build_sc_ctx()
        await self._populate_sc_ctx(markets, sc_ctx)

        for strategy, strat_cfg in self._strategies:
            # Build per-strategy context
            if strategy.name == "safe_compounder":
                ctx = sc_ctx
            else:
                ctx = {}  # AiDirectional needs no extra ctx

            try:
                candidates = await strategy.scan(markets, ctx)
            except Exception as exc:
                logger.warning("[%s] scan error: %s", strategy.name, exc)
                continue

            for candidate in candidates:
                order = self.decider.decide(candidate)
                if order is None:
                    self.store.record_signal(candidate, placed=False)
                    continue

                self.store.record_signal(candidate, placed=True)

                # Compute stop_loss / take_profit for AI candidates
                stop_loss: float | None = None
                take_profit: float | None = None
                ai_cfg = getattr(self._cfg, "ai_directional", None)
                if candidate.ai_probability is not None and ai_cfg is not None:
                    sl_pct = getattr(ai_cfg, "stop_loss_pct", 0.30)
                    tp_pct = getattr(ai_cfg, "take_profit_pct", 0.50)
                    stop_loss = order.price * (1.0 - sl_pct)
                    take_profit = order.price * (1.0 + tp_pct)

                mode = strat_cfg.mode
                await self.executor.place(order, mode=mode, stop_loss=stop_loss, take_profit=take_profit)

        await self.tracker.sweep(now=datetime.now(timezone.utc), max_hold_hours=self._max_hold_hours)

    async def run_forever(self) -> None:
        """Loop run_once every scan_interval_seconds; catches all exceptions."""
        self._running = True
        interval = self._cfg.scan_interval_seconds
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("[directional] run_once failed (continuing): %s", exc, exc_info=True)
            if interval > 0:
                await asyncio.sleep(interval)
