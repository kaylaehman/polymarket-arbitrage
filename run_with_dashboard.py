#!/usr/bin/env python3
"""
Run Trading Bot with Dashboard
===============================

Starts the trading bot and web dashboard together.
Supports cross-platform arbitrage between Polymarket and Kalshi.

Usage:
    python run_with_dashboard.py              # Dry run mode
    python run_with_dashboard.py --live       # Live mode
    python run_with_dashboard.py --port 8080  # Custom port
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
from datetime import datetime

import uvicorn

from polymarket_client import PolymarketClient
from polymarket_client.api import BasePolymarketClient
from kalshi_client import KalshiClient
from core.data_feed import DataFeed
from core.arb_engine import ArbEngine, ArbConfig
from core.execution import ExecutionEngine, ExecutionConfig
from core.risk_manager import RiskManager, RiskConfig
from core.portfolio import Portfolio
from core.cross_platform_arb import CrossPlatformArbEngine, MarketMatcher
from core.cross_platform_execution import CrossPlatformExecutor, CrossExecConfig
from polymarket_client.models import Market, MarketState
from utils.config_loader import load_config, BotConfig
try:
    from polymarket_us_client import PolymarketUSClient
except ImportError:
    PolymarketUSClient = None  # type: ignore
from utils.logging_utils import setup_logging
from dashboard.server import app, dashboard_state
from dashboard.integration import DashboardIntegration


logger = logging.getLogger(__name__)


class TradingBotWithDashboard:
    """Trading bot with integrated dashboard."""
    
    def __init__(self, config: BotConfig, port: int = 8888):
        self.config = config
        self.port = port
        self._running = False
        
        # Components - Polymarket
        self.client = None
        self.data_feed = None
        self.arb_engine = None
        self.execution_engine = None
        self.risk_manager = None
        self.portfolio = None
        self.dashboard_integration = None
        
        # Components - Kalshi (cross-platform)
        self.kalshi_client = None
        self.cross_platform_engine = None
        self.market_matcher = None
        self.cross_executor = None
        self.kalshi_arb_engine = None
        self.kalshi_execution_engine = None
        self._kalshi_markets = []
        self._matched_pairs = []
        self.cross_monitor = None
        self._monitor_kalshi = None

        # Intelligence layer (optional, annotate-only)
        self.intelligence_engine = None

        # Signal database (optional, append-only persistence)
        self.signal_db = None
        self.outcome_poller = None

        # External-agent control (optional, e.g. OpenClaw)
        self.agent_controller = None

        # Directional trading engine (optional, gated by config.directional.enabled)
        self.directional_engine = None

        # Server
        self._server = None
        self._server_task = None

    async def _guarded(self, coro, name: str) -> None:
        """Run a coroutine and catch + log any exception (loop crash isolation)."""
        try:
            await coro
        except Exception as e:
            logger.error(f"[{name}] loop crashed (isolated): {e}", exc_info=True)

    async def start(self) -> None:
        """Start the bot and dashboard."""
        logger.info("=" * 60)
        logger.info("Polymarket + Kalshi Arbitrage Bot")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DRY RUN' if self.config.is_dry_run else 'LIVE'}")
        logger.info(f"Cross-Platform: {'ENABLED' if self.config.mode.cross_platform_enabled else 'DISABLED'}")
        logger.info(f"Dashboard: http://localhost:{self.port}")
        logger.info("=" * 60)
        
        self._running = True
        
        # Initialize Polymarket API client
        if getattr(self.config.mode, "polymarket_us_enabled", False) and PolymarketUSClient is not None:
            logger.info("Initializing Polymarket.US client (polymarket_us_enabled=true)")
            self.client = PolymarketUSClient(
                key_id=self.config.api.polymarket_us_key_id,
                secret_key=self.config.api.polymarket_us_secret_key,
                dry_run=self.config.is_dry_run,
                rest_url=self.config.api.polymarket_us_rest_url,
                gateway_url=self.config.api.polymarket_us_gateway_url,
                timeout=self.config.api.timeout_seconds,
            )
        else:
            self.client = PolymarketClient(
                rest_url=self.config.api.polymarket_rest_url,
                ws_url=self.config.api.polymarket_ws_url,
                gamma_url=self.config.api.gamma_api_url,
                api_key=self.config.api.api_key,
                api_secret=self.config.api.api_secret,
                passphrase=self.config.api.passphrase,
                private_key=self.config.api.private_key,
                signature_type=self.config.api.signature_type,
                funder=self.config.api.funder,
                timeout=self.config.api.timeout_seconds,
                # Read-only (simulated) Polymarket unless a real polymarket.com wallet
                # key is set. Placeholder/empty key must NOT attempt live CLOB auth.
                dry_run=self.config.is_dry_run or self.config.api.private_key in (None, "", "YOUR_PRIVATE_KEY_HERE"),
            )
        await self.client.connect()
        
        # Initialize Kalshi client (if cross-platform enabled)
        if self.config.mode.cross_platform_enabled and self.config.mode.kalshi_enabled:
            logger.info("Initializing Kalshi client for cross-platform arbitrage...")
            self.kalshi_client = KalshiClient(
                timeout=self.config.api.timeout_seconds,
                max_retries=self.config.api.max_retries,
                dry_run=self.config.is_dry_run,
                api_key_id=self.config.api.kalshi_api_key_id,
                private_key_pem=self.config.api.kalshi_private_key,
            )
            
            # Initialize cross-platform arbitrage engine
            self.cross_platform_engine = CrossPlatformArbEngine(
                min_edge=self.config.trading.min_edge,
            )
            self.market_matcher = self.cross_platform_engine.matcher
            
            # Start Kalshi monitoring in background
            asyncio.create_task(self._start_kalshi_monitoring())
        
        # Initialize portfolio
        initial_balance = (
            self.config.mode.dry_run_initial_balance 
            if self.config.is_dry_run 
            else 0.0
        )
        self.portfolio = Portfolio(initial_balance=initial_balance)
        
        # Initialize risk manager
        self.risk_manager = RiskManager(RiskConfig(
            max_position_per_market=self.config.risk.max_position_per_market,
            max_global_exposure=self.config.risk.max_global_exposure,
            max_daily_loss=self.config.risk.max_daily_loss,
            max_drawdown_pct=self.config.risk.max_drawdown_pct,
            trade_only_high_volume=self.config.risk.trade_only_high_volume,
            min_24h_volume=self.config.risk.min_24h_volume,
            whitelist=self.config.risk.whitelist,
            blacklist=self.config.risk.blacklist,
            kill_switch_enabled=self.config.risk.kill_switch_enabled,
        ))
        
        # Initialize execution engine
        self.execution_engine = ExecutionEngine(
            client=self.client,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            config=ExecutionConfig(
                slippage_tolerance=self.config.trading.slippage_tolerance,
                order_timeout_seconds=self.config.trading.order_timeout_seconds,
                # Polymarket (polymarket.com CLOB) needs a Polygon wallet key to
                # trade. With no key set, force simulation even in live mode so the
                # Polymarket side stays read-only (Kalshi-only / Polymarket.US users).
                dry_run=self.config.is_dry_run or self.config.api.private_key in (None, "", "YOUR_PRIVATE_KEY_HERE"),
                kelly_enabled=self.config.trading.kelly_enabled,
                kelly_fraction=self.config.trading.kelly_fraction,
                kelly_max_fraction=self.config.trading.kelly_max_fraction,
                min_order_size=self.config.trading.min_order_size,
                max_order_size=self.config.trading.max_order_size,
            ),
        )
        await self.execution_engine.start()
        
        # Initialize arb engine
        self.arb_engine = ArbEngine(ArbConfig(
            min_edge=self.config.trading.min_edge,
            bundle_arb_enabled=self.config.trading.bundle_arb_enabled,
            min_spread=self.config.trading.min_spread,
            mm_enabled=self.config.trading.mm_enabled,
            tick_size=self.config.trading.tick_size,
            default_order_size=self.config.trading.default_order_size,
            min_order_size=self.config.trading.min_order_size,
            max_order_size=self.config.trading.max_order_size,
            time_decay_enabled=self.config.trading.time_decay_enabled,
            skip_if_resolves_within_hours=self.config.trading.skip_if_resolves_within_hours,
        ))

        # Initialize intelligence layer (optional; annotate-only, never blocks trades)
        try:
            from intelligence.intelligence_engine import build_engine
            self.intelligence_engine = build_engine(self.config.intelligence)
            if self.intelligence_engine is not None:
                logger.info("[Intelligence] Engine initialized (annotate-only)")
        except Exception as e:
            logger.warning(f"[Intelligence] init failed, continuing without: {e}")
            self.intelligence_engine = None

        # Alerts (gated; never raises into startup)
        alerts_cfg = getattr(self.config, "alerts", None)
        if alerts_cfg is not None and alerts_cfg.enabled:
            try:
                import os
                from core.alerts import Alerter
                from core import alerts as _alerts_mod
                _alerter = Alerter(
                    discord_webhook=os.getenv("ALERT_DISCORD_WEBHOOK"),
                    telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN"),
                    telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID"),
                    cooldown_seconds=alerts_cfg.cooldown_seconds,
                    min_severity=alerts_cfg.min_severity,
                )
                _alerts_mod.configure(_alerter)
                logger.info("[Alerts] Configured (enabled=true)")
            except Exception as _alerts_err:
                logger.warning(f"[Alerts] init failed, continuing without: {_alerts_err}")

        # Directional trading engine (gated; never touches arb loop)
        directional_cfg = getattr(self.config, "directional", None)
        if directional_cfg is None:
            logger.warning("config.directional absent — directional trading disabled")
        elif directional_cfg.enabled:
            from core.directional.engine import DirectionalEngine
            self.directional_engine = DirectionalEngine(
                directional_cfg,
                self.kalshi_client,
                self.intelligence_engine,
                self.risk_manager,
            )
            # Wire catalyst config into the directional scanner (additive, gated)
            _cat_cfg = getattr(self.config, "catalyst", None)
            if _cat_cfg is not None and _cat_cfg.enabled:
                self.directional_engine.scanner._catalyst_enabled = True
                self.directional_engine.scanner._catalyst_calendar = list(_cat_cfg.calendar or [])
                self.directional_engine.scanner._catalyst_window_hours = _cat_cfg.window_hours
                logger.info(
                    f"[Catalyst] Directional scanner wired: {len(_cat_cfg.calendar or [])} "
                    f"entries, window={_cat_cfg.window_hours}h"
                )
            asyncio.create_task(
                self._guarded(self.directional_engine.run_forever(), "directional")
            )
            dashboard_state.directional_store = self.directional_engine.store
            logger.info("[Directional] Engine launched (paper-gated)")

        # Initialize signal database (optional; append-only persistence)
        if self.config.database.enabled:
            try:
                from utils.signal_db import SignalDB
                self.signal_db = SignalDB(db_path=self.config.database.path)
                logger.info(f"[SignalDB] Logging to {self.config.database.path}")
            except Exception as e:
                logger.warning(f"[SignalDB] init failed, continuing without: {e}")
                self.signal_db = None

        # Start the outcome poller (records resolutions so accuracy can be measured)
        if self.signal_db is not None and self.config.database.auto_log_outcomes:
            try:
                from utils.outcome_poller import OutcomePoller
                self.outcome_poller = OutcomePoller(self.client, self.signal_db)
                await self.outcome_poller.start()
            except Exception as e:
                logger.warning(f"[OutcomePoller] init failed, continuing without: {e}")
                self.outcome_poller = None

        # Initialize data feed
        market_ids = self.config.trading.markets.copy()
        self.data_feed = DataFeed(
            client=self.client,
            market_ids=market_ids,
            position_refresh_interval=5.0,
            on_update=self._on_market_update,
            config=self.config,
        )
        await self.data_feed.start()
        
        # Initialize dashboard integration
        self.dashboard_integration = DashboardIntegration(
            data_feed=self.data_feed,
            arb_engine=self.arb_engine,
            execution_engine=self.execution_engine,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            mode="dry_run" if self.config.is_dry_run else "live",
        )
        await self.dashboard_integration.start()

        # Mount the external-agent control API (e.g. OpenClaw) if enabled.
        # The surface still requires AGENT_API_TOKEN at runtime or it returns 503.
        if self.config.agent.enabled:
            try:
                from core.agent_control import AgentController
                from dashboard.agent_api import router as agent_router, set_controller
                self.agent_controller = AgentController(
                    portfolio=self.portfolio,
                    risk_manager=self.risk_manager,
                    execution_engine=self.execution_engine,
                    signal_db=self.signal_db,
                    dashboard=dashboard_state,
                    mode="dry_run" if self.config.is_dry_run else "live",
                )
                set_controller(self.agent_controller, allow_control=self.config.agent.allow_control)
                app.include_router(agent_router)
                logger.info(
                    "[AgentAPI] Control surface mounted at /api/agent "
                    f"(control={'on' if self.config.agent.allow_control else 'read-only'})"
                )
            except Exception as e:
                logger.warning(f"[AgentAPI] failed to mount, continuing without: {e}")
                self.agent_controller = None

        # Start fill simulation for dry run
        if self.config.is_dry_run and self.config.mode.simulate_fills:
            asyncio.create_task(self._simulate_fills())
        
        # Start the web server
        await self._start_server()
        
        logger.info("Bot and dashboard started successfully!")
        logger.info(f"Open http://localhost:{self.port} in your browser")
    
    async def _start_server(self) -> None:
        """Start the uvicorn server."""
        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._server_task = asyncio.create_task(self._server.serve())
    
    def _on_market_update(self, market_id: str, market_state) -> None:
        """Handle market updates."""
        if not self._running:
            return

        # Paused by the external agent control API — stop submitting new signals.
        if self.agent_controller is not None and self.agent_controller.paused:
            return

        # Check risk limits
        if not self.risk_manager.within_global_limits():
            return
        
        # Analyze for opportunities
        signals = self.arb_engine.analyze(market_state)
        
        for signal in signals:
            # Add to dashboard
            if signal.opportunity:
                self.dashboard_integration.add_opportunity(
                    opportunity_type=signal.opportunity.opportunity_type.value,
                    market_id=signal.market_id,
                    edge=signal.opportunity.edge,
                    suggested_size=signal.opportunity.suggested_size,
                )
            
            self.dashboard_integration.add_signal(
                action=signal.action,
                market_id=signal.market_id,
            )

            # Submit to execution. When intelligence is enabled, annotate the
            # opportunity with an advisory AI signal first (never blocks the trade).
            if self.intelligence_engine is not None and self.config.intelligence.enabled:
                asyncio.create_task(self._annotate_and_submit(signal, market_state))
            else:
                asyncio.create_task(self.execution_engine.submit_signal(signal))

    async def _annotate_and_submit(self, signal, market_state) -> None:
        """Attach an advisory AI signal to the opportunity, then submit it.

        Annotate-only: the signal is logged and stored on the opportunity for the
        dashboard, but never blocks execution. Any failure is swallowed so the
        trade proceeds exactly as it would without the intelligence layer.
        """
        try:
            opp = signal.opportunity
            if opp is not None:
                summary = await self.intelligence_engine.evaluate(
                    market_id=signal.market_id,
                    market_question=market_state.market.question or signal.market_id,
                    current_yes_price=self._yes_price_from_book(market_state.order_book),
                    arb_edge=opp.edge,
                    resolution_criteria=market_state.market.description or None,
                )
                opp.signal = summary
                if summary.signal is not None:
                    logger.info(f"[Intelligence] {signal.market_id}: {summary.reason}")
                    if summary.should_filter:
                        logger.info(
                            f"[Intelligence] (annotate-only) would filter "
                            f"{signal.market_id} — proceeding anyway"
                        )
                    dashboard_state.add_ai_signal({
                        "market": signal.market_id,
                        "direction": summary.signal.direction,
                        "confidence": summary.signal.confidence,
                        "reason": summary.reason,
                    })
                self._persist_to_db(summary, opp)
        except Exception as e:
            logger.warning(f"[Intelligence] annotate failed for {signal.market_id}: {e}")

        await self.execution_engine.submit_signal(signal)

    def _persist_to_db(self, summary, opp) -> None:
        """Append the signal + opportunity to the SQLite store, if enabled."""
        if self.signal_db is None:
            return
        try:
            signal_id = None
            if summary.signal is not None and self.config.database.log_signals:
                signal_id = self.signal_db.log_signal(summary.signal, platform="polymarket")
            if self.config.database.log_opportunities:
                self.signal_db.log_opportunity(opp, signal_id=signal_id)
        except Exception as e:
            logger.warning(f"[SignalDB] failed to persist {opp.market_id}: {e}")

    @staticmethod
    def _yes_price_from_book(order_book) -> float:
        """Best estimate of the current YES price: mid, else a side, else 0.5."""
        bid = order_book.best_bid_yes
        ask = order_book.best_ask_yes
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return bid or ask or 0.5
    
    async def _simulate_fills(self) -> None:
        """Simulate order fills in dry run mode (Polymarket + Kalshi engines)."""
        import random

        # (execution_engine, client) pairs to simulate fills for.
        def _pairs():
            yield self.execution_engine, self.client
            if self.kalshi_execution_engine and self.kalshi_client:
                yield self.kalshi_execution_engine, self.kalshi_client

        while self._running:
            try:
                await asyncio.sleep(2.0)

                for engine, client in _pairs():
                    for order in engine.get_open_orders():
                        if random.random() < self.config.mode.fill_probability:
                            trade = client.simulate_fill(order.order_id)
                            if trade:
                                engine.handle_fill(trade)
                                self.dashboard_integration.add_trade(
                                    side=trade.side.value,
                                    price=trade.price,
                                    size=trade.size,
                                    market_id=trade.market_id,
                                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Fill simulation error: {e}")
    
    async def _start_kalshi_monitoring(self) -> None:
        """Start monitoring Kalshi markets for cross-platform arbitrage."""
        if not self.kalshi_client:
            return
        
        logger.info("Starting Kalshi market monitoring...")
        
        async with self.kalshi_client:
            # Set up dashboard for loading state
            dashboard_state.cross_platform["enabled"] = True
            dashboard_state.cross_platform["matching_status"] = "loading"
            
            # Fetch Kalshi markets with progress updates
            logger.info("Fetching Kalshi markets...")
            
            def on_kalshi_progress(count):
                dashboard_state.cross_platform["kalshi_markets"] = count
            
            self._kalshi_markets = await self.kalshi_client.list_all_markets(
                status="open",
                max_markets=5000,
                on_progress=on_kalshi_progress,
            )
            logger.info(f"✓ Loaded {len(self._kalshi_markets)} Kalshi markets")
            
            # Update dashboard state
            dashboard_state.cross_platform["kalshi_markets"] = len(self._kalshi_markets)
            
            # Wait for at least SOME Polymarket markets to load (start matching quickly!)
            logger.info("Waiting for Polymarket markets...")
            for i in range(30):  # Wait up to 30 seconds
                await asyncio.sleep(1)
                poly_count = len(self.data_feed._markets) if self.data_feed else 0
                
                # Update dashboard with current loading progress
                dashboard_state.cross_platform["polymarket_markets"] = poly_count
                
                # Start matching as soon as we have some markets from both platforms
                if poly_count >= 50:
                    logger.info(f"Got {poly_count} Polymarket markets - starting matching!")
                    break
                    
                if i % 5 == 0:
                    logger.info(f"Polymarket: {poly_count} markets loaded...")
            
            # Match markets between platforms (run in background so dashboard stays responsive)
            if self.data_feed and self._kalshi_markets:
                polymarket_markets = list(self.data_feed._markets.values())
                logger.info(f"Starting background matching: {len(polymarket_markets)} Polymarket x {len(self._kalshi_markets)} Kalshi")
                
                # Set initial status
                dashboard_state.cross_platform["matching_status"] = "starting"

                # Run matching (its heavy work is offloaded to a thread, so the
                # event loop stays responsive). We AWAIT it here — rather than
                # fire-and-forget — so the Kalshi client stays open for the
                # arbitrage monitor loop that follows.
                await self._run_matching_background(polymarket_markets)

                # Run the live loops concurrently until shutdown (keeps the
                # `async with self.kalshi_client` context open):
                #  - cross-platform detection/execution (Poly<->Kalshi)
                #  - Kalshi-native bundle-arb trading (single-venue, opt-in)
                loops = [self._monitor_cross_platform_arbs()]
                if self.config.mode.kalshi_native_enabled:
                    loops.append(self._run_kalshi_trading())
                await asyncio.gather(*loops)
    
    async def _run_matching_background(self, polymarket_markets: list) -> None:
        """Run market matching in a thread pool so dashboard stays fully responsive."""
        import concurrent.futures
        
        try:
            dashboard_state.cross_platform["matching_status"] = "matching"
            total = len(polymarket_markets) * len(self._kalshi_markets)
            dashboard_state.cross_platform["matching_total"] = total
            
            # Run matching in thread pool to avoid blocking event loop
            loop = asyncio.get_event_loop()
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            
            def run_matching_sync():
                """Synchronous matching that runs in thread."""
                import asyncio
                # Create new event loop for this thread
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                
                try:
                    def on_progress(checked, total, matches_found):
                        dashboard_state.cross_platform["matching_checked"] = checked
                        dashboard_state.cross_platform["matching_progress"] = int(checked / total * 100) if total > 0 else 0
                        dashboard_state.cross_platform["matched_pairs"] = matches_found
                        
                        # Update display data incrementally (show latest matches)
                        cached_pairs = self.market_matcher.get_cached_pairs()
                        if cached_pairs:
                            display_data = []
                            for pair in cached_pairs[-50:]:  # Show latest 50
                                display_data.append({
                                    "poly_question": pair.polymarket_question,
                                    "kalshi_title": pair.kalshi_title,
                                    "similarity": pair.similarity_score,
                                    "category": pair.category,
                                })
                            dashboard_state.cross_platform["matched_pairs_data"] = display_data
                    
                    result = new_loop.run_until_complete(
                        self.market_matcher.find_matches(
                            polymarket_markets,
                            self._kalshi_markets,
                            on_progress=on_progress,
                        )
                    )
                    return result
                finally:
                    new_loop.close()
            
            self._matched_pairs = await loop.run_in_executor(executor, run_matching_sync)
            
            dashboard_state.cross_platform["matching_status"] = "complete"
            dashboard_state.cross_platform["matching_progress"] = 100
            dashboard_state.cross_platform["matched_pairs"] = len(self._matched_pairs)
            
            logger.info(f"✓ Matching complete! Found {len(self._matched_pairs)} pairs")

            # Start live cross-platform arb monitoring (annotate-only, human review).
            # Uses a dedicated long-lived Kalshi client for orderbook polling.
            try:
                from kalshi_client import KalshiClient
                from core.cross_platform_monitor import CrossPlatformMonitor
                self._monitor_kalshi = KalshiClient(
                    timeout=self.config.api.timeout_seconds, dry_run=self.config.is_dry_run,
                )
                await self._monitor_kalshi.__aenter__()
                self.cross_monitor = CrossPlatformMonitor(
                    engine=self.cross_platform_engine,
                    data_feed=self.data_feed,
                    kalshi_client=self._monitor_kalshi,
                    get_pairs=lambda: self._matched_pairs,
                    intelligence_engine=self.intelligence_engine,
                    intel_enabled=self.config.intelligence.enabled,
                    signal_db=self.signal_db,
                    dashboard=dashboard_state,
                )
                await self.cross_monitor.start()
                logger.info("[CrossMonitor] Live cross-platform arb monitoring started")
            except Exception as e:
                logger.warning(f"[CrossMonitor] failed to start: {e}")
            
            # Prepare matched pairs data for dashboard display
            matched_pairs_display = []
            for pair in self._matched_pairs[:50]:
                matched_pairs_display.append({
                    "poly_question": pair.polymarket_question,
                    "kalshi_title": pair.kalshi_title,
                    "similarity": pair.similarity_score,
                    "category": pair.category,
                })
            
            dashboard_state.cross_platform["matched_pairs_data"] = matched_pairs_display
            
        except Exception as e:
            logger.error(f"Matching error: {e}")
            import traceback
            traceback.print_exc()
            dashboard_state.cross_platform["matching_status"] = "error"

    async def _monitor_cross_platform_arbs(self) -> None:
        """
        Continuously poll matched pairs for live cross-platform arbitrage and
        route any opportunity to the atomic two-leg executor.

        Detection/alerting always runs. Whether orders are actually placed is
        gated by mode.cross_platform_execution_enabled (and dry_run simulates).
        """
        pairs = sorted(
            self._matched_pairs or [],
            key=lambda p: p.similarity_score,
            reverse=True,
        )[: self.config.monitoring.cross_platform_max_pairs]

        if not pairs or not self.kalshi_client or not self.cross_platform_engine:
            logger.info("Cross-platform monitor: no matched pairs to watch.")
            return

        self.cross_executor = CrossPlatformExecutor(
            poly_client=self.client,
            kalshi_client=self.kalshi_client,
            risk_manager=self.risk_manager,
            portfolio=self.portfolio,
            config=CrossExecConfig(
                dry_run=self.config.is_dry_run,
                execution_enabled=self.config.mode.cross_platform_execution_enabled,
                max_trade_notional=self.config.trading.cross_platform_max_trade_notional,
            ),
        )

        poll = self.config.monitoring.cross_platform_poll_seconds
        logger.info(
            f"Cross-platform arb monitor started: {len(pairs)} pairs, every {poll}s "
            f"(execution={'ON' if self.config.mode.cross_platform_execution_enabled else 'OFF — detect only'})"
        )

        while self._running:
            found = 0
            for pair in pairs:
                if not self._running:
                    break
                try:
                    poly_ob = await self.client.get_orderbook(pair.polymarket_id)
                    kalshi_ob = await self.kalshi_client.get_orderbook_unified(pair.kalshi_ticker)
                    if not poly_ob or not kalshi_ob:
                        continue

                    opp = self.cross_platform_engine.check_arbitrage(pair, poly_ob, kalshi_ob)
                    if not opp:
                        continue

                    found += 1
                    dashboard_state.add_cross_platform_opportunity({
                        "poly_question": pair.polymarket_question,
                        "kalshi_title": pair.kalshi_title,
                        "token": opp.token,
                        "buy_platform": opp.buy_platform,
                        "sell_platform": opp.sell_platform,
                        "buy_price": round(opp.buy_price, 4),
                        "sell_price": round(opp.sell_price, 4),
                        "net_edge": round(opp.net_edge, 4),
                        "edge_pct": round(opp.edge_pct, 4),
                        "suggested_size": opp.suggested_size,
                    })

                    # Full two-leg arb if enabled; else Kalshi-only directional
                    # (oracle) leg if that's enabled; else detect-only.
                    if self.config.mode.cross_platform_execution_enabled:
                        result = await self.cross_executor.execute(opp)
                        logger.info(f"Cross-platform exec [{result.status}]: {result.detail}")
                    elif self.config.mode.kalshi_oracle_enabled:
                        result = await self.cross_executor.execute_kalshi_leg_only(opp)
                        logger.info(f"Kalshi oracle exec [{result.status}]: {result.detail}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"Cross-platform check failed for {pair.pair_id}: {e}")

                # Throttle between pairs to stay under venue rate limits.
                await asyncio.sleep(0.15)

            if found:
                logger.info(f"Cross-platform sweep: {found} opportunities across {len(pairs)} pairs")
            await asyncio.sleep(poll)

    async def _select_kalshi_arb_markets(self):
        """Select Kalshi markets for bundle-arb watching.

        Tries to use KalshiMarketScanner (events endpoint, real orderbooks) so
        the arb engine watches LIQUID markets with actual two-sided books.
        max_spread=0.99 disables the spread filter so bundle dislocations (any
        spread) can be detected.

        Falls back to the old volume-sort on self._kalshi_markets if the scan
        fails for any reason.  self._kalshi_markets is NEVER modified here so
        the cross-platform matcher continues to work.
        """
        try:
            from core.directional.scanner import KalshiMarketScanner
            from utils.kalshi_categories import categorize
            scanner = KalshiMarketScanner(
                self.kalshi_client,
                categorize,
                min_volume=0,
                exclude_categories=[],
                max_spread=0.99,
            )
            # Wire catalyst config if enabled (additive, gated)
            _cat_cfg = getattr(self.config, "catalyst", None)
            if _cat_cfg is not None and _cat_cfg.enabled:
                scanner._catalyst_enabled = True
                scanner._catalyst_calendar = list(_cat_cfg.calendar or [])
                scanner._catalyst_window_hours = _cat_cfg.window_hours
            liquid = await scanner.scan(self.config.monitoring.kalshi_max_markets)
            if liquid:
                return liquid
        except Exception as e:
            logger.warning(f"[KalshiArb] liquid-market scan failed, falling back: {e}")
        # Fallback: previous behaviour (volume sort over the macro/parlay set)
        return sorted(
            self._kalshi_markets,
            key=lambda m: getattr(m, "volume", 0) or 0,
            reverse=True,
        )[: self.config.monitoring.kalshi_max_markets]

    async def _run_kalshi_trading(self) -> None:
        """
        Kalshi-native single-venue bundle arbitrage (for Kalshi-only users).

        Reuses the venue-agnostic ArbEngine + ExecutionEngine, pointed at Kalshi
        order books and the Kalshi client. Detects riskless bundle arbs (YES+NO
        priced under $1 after fees) and trades them on Kalshi alone. Simulates in
        dry_run; places real Kalshi orders only in live mode.
        """
        if not self.kalshi_client or not self._kalshi_markets:
            return

        # Dedicated engines so Kalshi trading is independent of the Polymarket path.
        self.kalshi_arb_engine = ArbEngine(ArbConfig(
            min_edge=self.config.trading.min_edge,
            bundle_arb_enabled=self.config.trading.bundle_arb_enabled,
            mm_enabled=False,  # bundle arb only — riskless single-venue
            tick_size=self.config.trading.tick_size,
            default_order_size=self.config.trading.default_order_size,
            min_order_size=self.config.trading.min_order_size,
            max_order_size=self.config.trading.max_order_size,
        ))
        self.kalshi_execution_engine = ExecutionEngine(
            client=self.kalshi_client,
            risk_manager=self.risk_manager,   # shared global exposure budget
            portfolio=self.portfolio,
            config=ExecutionConfig(
                slippage_tolerance=self.config.trading.slippage_tolerance,
                order_timeout_seconds=self.config.trading.order_timeout_seconds,
                dry_run=self.config.is_dry_run,
            ),
        )
        await self.kalshi_execution_engine.start()

        # Watch the most liquid Kalshi markets.
        # _select_kalshi_arb_markets tries the events-endpoint scanner first so
        # we get markets with real orderbooks; falls back to the old sort if the
        # scan fails.  self._kalshi_markets is untouched (cross-platform still works).
        watched = await self._select_kalshi_arb_markets()
        poll = self.config.monitoring.kalshi_poll_seconds
        mode = "LIVE" if self.config.is_live else "dry_run"
        logger.info(f"Kalshi-native bundle-arb trading started: {len(watched)} markets, every {poll}s ({mode})")

        # ── WS real-time feed (additive, gated, REST sweep always retained) ──
        ws_client = None
        if getattr(self.config.monitoring, "kalshi_ws_enabled", False):
            try:
                import time as _time
                from kalshi_client.ws import KalshiWSClient
                from core.kalshi_ws_detector import WSBundleDetector, decide_detection_mode
                titles = {m.ticker: m.title for m in watched}
                detector = WSBundleDetector(
                    self.kalshi_arb_engine,
                    self.kalshi_execution_engine,
                    titles,
                )
                ws_client = KalshiWSClient(
                    self.kalshi_client,
                    on_book_update=detector.on_book_update,
                )
                asyncio.create_task(
                    self._guarded(ws_client.run([m.ticker for m in watched]), "kalshi-ws")
                )
                logger.info(f"[KalshiWS] real-time feed enabled for {len(watched)} tickers")
            except Exception as _ws_err:
                logger.warning(f"[KalshiWS] setup failed — REST-only fallback: {_ws_err}")
                ws_client = None
        else:
            import time as _time

        prev_mode = "rest"

        while self._running:
            found = 0
            for km in watched:
                if not self._running:
                    break
                try:
                    ob = await self.kalshi_client.get_orderbook_unified(km.ticker)
                    if not ob:
                        continue
                    market_state = MarketState(
                        market=Market(market_id=f"kalshi:{km.ticker}", condition_id="", question=km.title),
                        order_book=ob,
                    )
                    signals = self.kalshi_arb_engine.analyze(market_state)
                    for signal in signals:
                        found += 1
                        if signal.opportunity:
                            self.dashboard_integration.add_opportunity(
                                opportunity_type=f"kalshi_{signal.opportunity.opportunity_type.value}",
                                market_id=signal.market_id,
                                edge=signal.opportunity.edge,
                                suggested_size=signal.opportunity.suggested_size,
                            )
                            # Fire-and-forget bundle alert (gated; never blocks loop)
                            try:
                                from core import alerts as _alerts
                                asyncio.create_task(
                                    _alerts.notify(
                                        "bundle",
                                        "Bundle arb signal",
                                        f"{signal.market_id} edge={signal.opportunity.edge:.4f}",
                                        severity="info",
                                        dedup_key=signal.market_id,
                                    )
                                )
                            except Exception:
                                pass
                        await self.kalshi_execution_engine.submit_signal(signal)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug(f"Kalshi-native check failed for {km.ticker}: {e}")
                await asyncio.sleep(0.1)

            if found:
                logger.info(f"Kalshi-native sweep: {found} bundle-arb signals across {len(watched)} markets")

            # ── sleep cadence: shorter when WS is healthy ──
            if ws_client is not None:
                cur_mode, reason = decide_detection_mode(
                    True,
                    ws_client.state,
                    ws_client.last_message_ts,
                    _time.monotonic(),
                    self.config.monitoring.ws_staleness_seconds,
                )
            else:
                cur_mode, reason = "rest", "rest:disabled"
            if cur_mode != prev_mode:
                logger.info(f"[KalshiWS] -> {cur_mode} ({reason})")
                prev_mode = cur_mode
            sweep_interval = (
                self.config.monitoring.ws_reconcile_seconds
                if cur_mode == "ws"
                else poll
            )
            await asyncio.sleep(sweep_interval)

    async def stop(self) -> None:
        """Stop everything gracefully."""
        logger.info("Shutting down...")
        self._running = False
        
        if self.dashboard_integration:
            await self.dashboard_integration.stop()
        
        if self.data_feed:
            await self.data_feed.stop()
        
        if self.execution_engine:
            await self.execution_engine.stop()

        if self.kalshi_execution_engine:
            await self.kalshi_execution_engine.stop()
        
        if self.cross_monitor:
            await self.cross_monitor.stop()

        if self._monitor_kalshi:
            try:
                await self._monitor_kalshi.__aexit__(None, None, None)
            except Exception:
                pass

        if self.outcome_poller:
            await self.outcome_poller.stop()

        if self.client:
            await self.client.disconnect()

        if self.signal_db:
            self.signal_db.close()

        # Kalshi client is closed via async context manager in _start_kalshi_monitoring
        
        if self._server:
            self._server.should_exit = True
        
        # Final summary
        if self.portfolio:
            summary = self.portfolio.get_summary()
            logger.info("=" * 60)
            logger.info("Final Summary")
            logger.info("=" * 60)
            logger.info(f"Total PnL: ${summary['pnl']['total_pnl']:.2f}")
            logger.info(f"Trades: {summary['total_trades']}")
            logger.info(f"Win Rate: {summary['win_rate']:.1%}")
        
        # Cross-platform summary
        if self.cross_platform_engine:
            cp_stats = self.cross_platform_engine.get_stats()
            logger.info(f"Cross-Platform Opportunities: {cp_stats['total_opportunities']}")
            logger.info(f"Matched Market Pairs: {cp_stats['matched_pairs']}")
        
        logger.info("Shutdown complete")
    
    async def run_forever(self) -> None:
        """Run until interrupted."""
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass


async def main_async(args: argparse.Namespace) -> None:
    """Async main function."""
    # Load config
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)
    
    # Override mode
    if args.live:
        config.mode.trading_mode = "live"
    elif args.dry_run:
        config.mode.trading_mode = "dry_run"
    
    # Create and run bot with dashboard
    bot = TradingBotWithDashboard(config, port=args.port)
    
    # Handle shutdown
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()
    
    def signal_handler():
        logger.info("Shutdown signal received")
        shutdown_event.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            pass
    
    try:
        await bot.start()
        
        # Wait for shutdown
        await shutdown_event.wait()
        
    except KeyboardInterrupt:
        pass
    finally:
        await bot.stop()


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Polymarket Arbitrage Bot with Live Dashboard"
    )
    
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Config file path"
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=8888,
        help="Dashboard port (default: 8888)"
    )
    
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live mode"
    )
    
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Run in dry-run mode (default)"
    )
    
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose logging"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    log_level = "DEBUG" if args.verbose else "INFO"
    setup_logging(console_level=log_level)
    
    # Run
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nShutdown complete.")


if __name__ == "__main__":
    main()

