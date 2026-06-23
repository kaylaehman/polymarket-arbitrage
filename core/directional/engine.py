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
import os
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from core.directional.decider import Decider
from core.directional.executor import Executor
from core.directional.scanner import KalshiMarketScanner
from core.directional.store import DirectionalStore
from core.directional.strategies.ai_directional import AiDirectional
from core.directional.strategies.maker_longshot import MakerLongshotStrategy
from core.directional.strategies.safe_compounder import SafeCompounder
from core.directional.tracker import Tracker
from utils.kalshi_categories import categorize
from core.directional.pmus_weather_source import PMUSWeatherSource
from polymarket_us_client.api import PolymarketUSClient

logger = logging.getLogger(__name__)


class DirectionalEngine:
    """Top-level loop that orchestrates the directional trading cycle.

    Args:
        config: The config.directional config namespace.
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

        # FIX C1: process-lived AV client — initialized lazily in _ensure_av_client()
        self._av_client = None
        self._av_http = None

        # Build store
        self.store = DirectionalStore(config.db_path)
        self.store.init_schema()

        # Build scanner (M1: min_volume is a proper field on DirectionalConfig).
        # Pass priority_series from config.scanner so backtest-validated weather +
        # macro series are enumerated directly and merged into the scan universe.
        scanner_cfg = getattr(config, "scanner", None)
        _priority_series = list(getattr(scanner_cfg, "priority_series", []) or []) if scanner_cfg else []
        _priority_max_days = getattr(scanner_cfg, "max_days_to_resolution", 30.0) if scanner_cfg else 30.0
        self.scanner = KalshiMarketScanner(
            kalshi_client=kalshi_client,
            categorize_fn=categorize,
            min_volume=config.min_volume,
            exclude_categories=list(config.category_exclude),
            priority_series=_priority_series,
            priority_series_max_days=_priority_max_days,
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
                    max_days_to_resolution=getattr(ai_cfg, "max_days_to_resolution", 45.0),
                    categories=list(getattr(ai_cfg, "categories", []) or []),
                ), ai_cfg)
            )

        ml_cfg = getattr(config, "maker_longshot", None)
        weather_cfg = getattr(config, "weather", None)
        if ml_cfg is not None:
            self._strategies.append(
                (MakerLongshotStrategy(
                    min_structural_score=ml_cfg.min_structural_score,
                    min_yes_price=ml_cfg.min_yes_price,
                    max_yes_price=ml_cfg.max_yes_price,
                    price_improvement_cents=ml_cfg.price_improvement_cents,
                    skip_categories=list(getattr(ml_cfg, "skip_categories", [])),
                    max_days_to_resolution=getattr(ml_cfg, "max_days_to_resolution", 90.0),
                    weather_cfg=weather_cfg,
                    financial_cfg=getattr(config, "financial", None),
                ), ml_cfg)
            )
        self._weather_cfg = weather_cfg
        self._financial_cfg = getattr(config, "financial", None)

        # PM.US weather source — config gated (default enabled=True)
        pmus_cfg = getattr(config, "pmus_weather", None)
        self._pmus_source: Optional[PMUSWeatherSource] = None
        self._pmus_client: Optional[PolymarketUSClient] = None
        if pmus_cfg is not None and getattr(pmus_cfg, "enabled", True):
            self._pmus_source = PMUSWeatherSource(
                http=None,  # http client injected per-cycle in run_once
                max_days=getattr(pmus_cfg, "max_days", 30.0),
                cache_ttl_seconds=getattr(pmus_cfg, "cache_ttl_seconds", 300.0),
            )
            # Lightweight unauthenticated client for resolution checks only;
            # no credentials needed — get_market_result hits the public gateway.
            self._pmus_client = PolymarketUSClient(dry_run=True)

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

        # Build tracker — pass PM.US client so it can resolve pmus: positions
        self.tracker = Tracker(
            store=self.store,
            kalshi_client=kalshi_client,
            executor=self.executor,
            risk_manager=risk_manager,
            pmus_client=self._pmus_client,
        )

        # Max hold hours for tracker (default 72)
        self._max_hold_hours = getattr(ai_cfg, "max_hold_hours", 72.0) if ai_cfg else 72.0
        # Maker TTL for tracker (default 60 min)
        self._order_ttl_minutes = getattr(ml_cfg, "order_ttl_minutes", 60.0) if ml_cfg else 60.0

    def _get_cash_balance(self) -> float:
        """Synchronous placeholder returning a fixed balance for Kelly sizing."""
        # In paper mode this doesn't affect live money; live mode uses risk caps.
        return 100.0

    def _ensure_av_client(self) -> None:
        """FIX C1: Lazily initialize the process-lived AVClient on first call.

        Subsequent calls are no-ops. The client and its httpx session persist
        across run_once() cycles so TTL caches are preserved.
        """
        if self._av_client is not None:
            return
        from core.market_data import AVClient
        av_api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "")
        if (av_api_key
                and self._financial_cfg is not None
                and self._financial_cfg.enabled):
            self._av_http = httpx.AsyncClient(timeout=30.0)
            max_calls = getattr(self._financial_cfg, "max_calls_per_day", 20)
            max_age = getattr(self._financial_cfg, "max_price_age_days", 3)
            self._av_client = AVClient(
                api_key=av_api_key,
                price_ttl_s=self._financial_cfg.price_ttl_minutes * 60,
                vol_ttl_s=self._financial_cfg.vol_ttl_hours * 3600,
                max_calls_per_day=max_calls,
                max_price_age_days=max_age,
                http=self._av_http,
            )

    async def close(self) -> None:
        """FIX C1: Close the process-lived AV HTTP client on engine shutdown."""
        if self._av_http is not None:
            await self._av_http.aclose()
            self._av_http = None
            self._av_client = None

    def _build_sc_ctx(self) -> dict:
        """Build SafeCompounder context using scanner's already-fetched books.

        The scanner populates scanner.last_books during scan(), so no second
        round of orderbook fetches is needed here.  The closure delegates to
        scanner.no_ask(ticker) which reads from scanner.last_books.
        """
        def no_ask_fn(ticker: str) -> float | None:
            return self.scanner.no_ask(ticker)

        return {"no_ask": no_ask_fn}

    async def run_once(self) -> None:
        """Execute one full scan → decide → execute → sweep cycle."""
        markets = await self.scanner.scan(self._cfg.markets_per_cycle)

        # SafeCompounder context — no_ask reads from scanner.last_books (no re-fetch)
        sc_ctx = self._build_sc_ctx()

        # Add an httpx client for the weather forecast gate when enabled.
        # The client is shared across all weather candidates in this cycle (TTL cache
        # in core.weather means NWS is called at most once per gridpoint per 15 min).
        weather_enabled = (
            self._weather_cfg is not None and self._weather_cfg.enabled
        )
        _http_client: Optional[Any] = None
        if weather_enabled:
            _http_client = httpx.AsyncClient(timeout=10.0)
            sc_ctx = {**sc_ctx, "http": _http_client}

        # FIX C1: use process-lived AVClient (lazily initialized, never closed per cycle).
        self._ensure_av_client()
        if self._av_client is not None:
            sc_ctx = {**sc_ctx, "av": self._av_client}

        # MakerLongshotStrategy needs the full pre-cap liquid universe so that
        # near-term longshots (e.g. KXCABLEAVE-26MAY22-26JUL at index 114) are not
        # silently dropped by the spread-sort -> cap(15) applied to the general list.
        # scanner.last_liquid holds all liquid+categorized markets before the cap;
        # no additional API calls are needed since last_books is already populated.
        maker_markets = self.scanner.last_liquid if self.scanner.last_liquid else markets

        # Augment maker_markets with PM.US weather markets if source is configured
        if self._pmus_source is not None and _http_client is not None:
            try:
                self._pmus_source._http = _http_client
                pmus_markets = await self._pmus_source.fetch()
                if pmus_markets:
                    # Build a no_ask lookup that covers both Kalshi and PM.US books
                    kalshi_no_ask = sc_ctx["no_ask"]
                    def _merged_no_ask(ticker: str) -> Optional[float]:
                        if ticker.startswith("pmus:"):
                            return self._pmus_source.no_ask(ticker)
                        return kalshi_no_ask(ticker)
                    sc_ctx = {**sc_ctx, "no_ask": _merged_no_ask}
                    maker_markets = list(maker_markets) + pmus_markets
                    logger.info("[pmus-weather] merged %d PM.US weather markets into maker universe", len(pmus_markets))
            except Exception as exc:
                logger.warning("[pmus-weather] source fetch failed (continuing without): %s", exc)

        # Build per-strategy dedup sets once per cycle so repeated scans of the
        # same market never stack duplicate open/pending positions.
        all_active = self.store.open_positions() + self.store.pending_positions()
        held_markets: dict[str, set[str]] = {}
        for pos in all_active:
            held_markets.setdefault(pos.strategy, set()).add(pos.market_id)

        for strategy, strat_cfg in self._strategies:
            # Build per-strategy context and market list
            if strategy.name == "maker_longshot":
                ctx = sc_ctx
                strategy_markets = maker_markets
            elif strategy.name == "safe_compounder":
                ctx = sc_ctx
                strategy_markets = markets
            else:
                ctx = {}  # AiDirectional needs no extra ctx
                strategy_markets = markets

            try:
                candidates = await strategy.scan(strategy_markets, ctx)
            except Exception as exc:
                logger.warning("[%s] scan error: %s", strategy.name, exc)
                continue

            strategy_held = held_markets.get(strategy.name, set())

            for candidate in candidates:
                # Dedup: skip if this strategy already holds an open/pending
                # position for this market_id (prevents stacking duplicates).
                if candidate.market_id in strategy_held:
                    logger.debug(
                        "[%s] skipping %s — already have an open/pending position",
                        strategy.name,
                        candidate.market_id,
                    )
                    continue

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
                # Track the newly placed market so subsequent candidates in this
                # same cycle don't re-post it (handles multiple candidates for
                # the same market within one scan batch).
                strategy_held.add(candidate.market_id)

        # Close the per-cycle weather HTTP client if we opened one.
        if _http_client is not None:
            await _http_client.aclose()
        # FIX C1: do NOT close _av_http here — it's process-lived.

        await self.tracker.sweep(
            now=datetime.now(timezone.utc),
            max_hold_hours=self._max_hold_hours,
            order_ttl_minutes=self._order_ttl_minutes,
        )

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
