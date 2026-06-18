"""
Cross-Platform Monitor
======================

Drives live cross-platform arbitrage detection: for each matched Polymarket↔Kalshi
pair, it gathers both order books, runs ``CrossPlatformArbEngine.check_arbitrage``,
and — when an opportunity is found — annotates it with an advisory AI signal,
logs it, and publishes it to the dashboard.

This is where the intelligence layer finally *fits*: cross-platform arbs are
directional ("which crowd is right?"), so a news-based signal is meaningful here
in a way it never was for riskless bundle arb. Still annotate-only — these are
flagged for human review, never auto-traded (no Kalshi order placement exists).
"""

import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class CrossPlatformMonitor:
    """Evaluates matched market pairs for cross-platform arbitrage + AI signal."""

    def __init__(
        self,
        engine,
        data_feed,
        kalshi_client,
        get_pairs: Callable[[], list],
        *,
        intelligence_engine=None,
        intel_enabled: bool = False,
        signal_db=None,
        dashboard=None,
        interval_seconds: float = 30.0,
        max_pairs: int = 200,
    ):
        """
        Args:
            engine: CrossPlatformArbEngine (provides ``check_arbitrage``).
            data_feed: provides ``get_order_book(polymarket_id)``.
            kalshi_client: provides ``async get_orderbook_unified(ticker)``.
            get_pairs: callable returning the current list of MarketPair objects.
            intelligence_engine: optional; annotates opportunities when enabled.
            intel_enabled: whether the intelligence layer is on.
            signal_db: optional SignalDB for persistence.
            dashboard: optional DashboardIntegration / dashboard_state-like object
                exposing ``add_cross_platform_opportunity(dict)``.
            interval_seconds: poll cadence.
            max_pairs: cap on pairs evaluated per pass (protects rate limits).
        """
        self.engine = engine
        self.data_feed = data_feed
        self.kalshi_client = kalshi_client
        self.get_pairs = get_pairs
        self.intelligence_engine = intelligence_engine
        self.intel_enabled = intel_enabled
        self.signal_db = signal_db
        self.dashboard = dashboard
        self.interval_seconds = interval_seconds
        self.max_pairs = max_pairs
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def evaluate_pair(self, pair):
        """Evaluate one matched pair. Returns a CrossPlatformOpportunity or None."""
        poly_ob = self.data_feed.get_order_book(pair.polymarket_id)
        if poly_ob is None:
            return None

        try:
            kalshi_ob = await self.kalshi_client.get_orderbook_unified(pair.kalshi_ticker)
        except Exception as e:  # noqa: BLE001 — skip this pair, retry next pass
            logger.debug("[CrossMonitor] kalshi OB failed for %s: %s", pair.kalshi_ticker, e)
            return None
        if kalshi_ob is None:
            return None

        opp = self.engine.check_arbitrage(pair, poly_ob, kalshi_ob)
        if opp is None:
            return None

        await self._annotate(opp, pair, poly_ob)
        self._publish(opp, pair)
        return opp

    async def _annotate(self, opp, pair, poly_ob) -> None:
        """Attach an advisory AI signal + persist. Never raises."""
        if not (self.intelligence_engine is not None and self.intel_enabled):
            return
        try:
            summary = await self.intelligence_engine.evaluate(
                market_id=pair.pair_id,
                market_question=pair.polymarket_question or pair.kalshi_title,
                current_yes_price=self._yes_price(poly_ob),
                arb_edge=opp.net_edge,
            )
            opp.signal = summary
            if summary.signal is not None:
                logger.info("[CrossMonitor] %s: %s", pair.pair_id, summary.reason)
            self._persist(opp, summary, pair)
        except Exception as e:  # noqa: BLE001 — advisory, must not break detection
            logger.warning("[CrossMonitor] annotate failed for %s: %s", pair.pair_id, e)

    def _persist(self, opp, summary, pair) -> None:
        if self.signal_db is None:
            return
        try:
            sid = None
            if summary.signal is not None:
                sid = self.signal_db.log_signal(summary.signal, platform="cross_platform")
            # Reuse the opportunities table via a lightweight shim object.
            self.signal_db.log_opportunity(
                _OppRow(market_id=pair.pair_id, opportunity_type="cross_platform",
                        edge=opp.net_edge, signal=summary),
                signal_id=sid,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[CrossMonitor] persist failed for %s: %s", pair.pair_id, e)

    def _publish(self, opp, pair) -> None:
        if self.dashboard is None:
            return
        try:
            sig = opp.signal.signal if (opp.signal and opp.signal.signal) else None
            self.dashboard.add_cross_platform_opportunity({
                "poly_question": pair.polymarket_question,
                "kalshi_title": pair.kalshi_title,
                "buy_platform": opp.buy_platform,
                "sell_platform": opp.sell_platform,
                "token": opp.token,
                "edge_pct": opp.edge_pct,
                "category": pair.category,
                # AI signal fields (None when intelligence is off)
                "ai_direction": sig.direction if sig else None,
                "ai_confidence": sig.confidence if sig else None,
                "ai_reason": opp.signal.reason if opp.signal else None,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("[CrossMonitor] publish failed: %s", e)

    async def poll_once(self) -> int:
        """Evaluate all current pairs once. Returns the count of opportunities found."""
        found = 0
        for pair in (self.get_pairs() or [])[: self.max_pairs]:
            opp = await self.evaluate_pair(pair)
            if opp is not None:
                found += 1
        return found

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="cross_platform_monitor")
        logger.info("[CrossMonitor] Started (every %.0fs)", self.interval_seconds)

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[CrossMonitor] poll error: %s", e)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @staticmethod
    def _yes_price(order_book) -> float:
        bid = order_book.best_bid_yes
        ask = order_book.best_ask_yes
        if bid is not None and ask is not None:
            return (bid + ask) / 2
        return bid or ask or 0.5


class _OppRow:
    """Minimal duck-typed opportunity for SignalDB.log_opportunity (cross-platform)."""

    def __init__(self, market_id, opportunity_type, edge, signal):
        self.market_id = market_id
        self.opportunity_type = opportunity_type
        self.edge = edge
        self.signal = signal
