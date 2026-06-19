"""Integration test — full paper run with both strategies.

Task 17: Tests the complete pipeline from market scanning through
strategy evaluation, decision, execution, and tracker sweep.

No live API calls. All market data, orderbooks, and resolution signals
come from in-process fakes.

Scenario:
  - Market A: KX-CHEAP-NO — YES price=0.05 → fair_no=0.95, no_ask=0.07
    → SafeCompounder emits a NO candidate with edge ~0.88 cents (> min 1 cent)
  - Market B: KX-AI-YES — AI says YES with confidence 0.80, edge 0.15
    → AiDirectional emits a YES candidate (passes min_confidence=0.70, min_edge=0.05)

After run_once() (with no markets resolved):
  - store has 2 open positions (1 safe_compounder + 1 ai_directional), both paper.
  - place_order NOT called.

Then set Market B as resolved=YES and call tracker.sweep() explicitly:
  - store has 1 open position (Market A remains open).
  - Market B (ai_directional YES) is closed with positive realized_pnl.
"""
import asyncio
import pytest
from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from core.directional.store import DirectionalStore
from core.directional.tracker import Tracker
from core.directional.executor import Executor
from core.directional.engine import DirectionalEngine


# ── Fake KalshiMarket ────────────────────────────────────────────────────────

def _market(ticker, yes_price, no_price, vol=1000, result=None):
    m = SimpleNamespace()
    m.ticker = ticker
    m.event_ticker = ticker
    m.title = f"Integration test market {ticker}"
    m.yes_price = yes_price
    m.no_price = no_price
    m.volume = vol
    m.category = "Finance"
    m.status = "open" if result is None else "settled"
    m.result = result
    m.to_unified_market_id = lambda t=ticker: f"kalshi:{t}"
    return m


# ── Fake orderbook ──────────────────────────────────────────────────────────

def _ob(no_ask: float):
    """Build a fake unified orderbook where NO best_ask == no_ask."""
    yes_side = SimpleNamespace(best_ask=1.0 - no_ask, best_bid=no_ask - 0.02, mid_price=no_ask - 0.01)
    no_side = SimpleNamespace(best_ask=no_ask, best_bid=no_ask - 0.01, mid_price=no_ask - 0.005)
    return SimpleNamespace(yes=yes_side, no=no_side)


# ── Fake intelligence engine ─────────────────────────────────────────────────

class SignalSummary:
    def __init__(self, direction, confidence, edge, ai_prob):
        self.signal = SimpleNamespace(
            direction=direction,
            confidence=confidence,
            edge_vs_market=edge,
            ai_probability=ai_prob,
            reasoning="integration test signal",
        )


class FakeIntel:
    """Returns a bullish signal for KX-AI-YES and None for everything else."""

    async def evaluate(self, market_id="", **kwargs):
        if "KX-AI-YES" in market_id:
            return SignalSummary("bullish", confidence=0.80, edge=0.15, ai_prob=0.75)
        return None


# ── Fake kalshi_client with mutable resolution state ─────────────────────────

class FakeKalshiClient:
    """Serves fixture markets and orderbooks; never calls a live API.

    The resolved dict is mutable so tests can set resolution state
    between run_once() and tracker.sweep() calls.
    """

    def __init__(self, markets, no_asks: dict):
        self._markets = markets
        self._no_asks = no_asks      # ticker → no_ask float
        self.resolved: dict = {}     # mutable: ticker → "yes"|"no"
        self.place_order_calls = []

    async def list_all_markets(self, status="open", max_markets=50):
        return [m for m in self._markets if m.status == "open"]

    async def get_orderbook_unified(self, ticker):
        # Handle "kalshi:KX-..." prefixed IDs from tracker
        ticker = ticker.split("kalshi:", 1)[-1] if "kalshi:" in ticker else ticker
        no_ask = self._no_asks.get(ticker)
        if no_ask is None:
            return None
        return _ob(no_ask)

    async def get_market(self, ticker):
        ticker = ticker.split("kalshi:", 1)[-1] if "kalshi:" in ticker else ticker
        for m in self._markets:
            if m.ticker == ticker:
                result = self.resolved.get(ticker)
                if result is not None:
                    m.result = result
                    m.status = "settled"
                else:
                    m.result = None
                    m.status = "open"
                return m
        return None

    async def place_order(self, **kwargs):
        self.place_order_calls.append(kwargs)
        return object()

    async def get_balance(self):
        return 100.0


# ── Fake risk manager ────────────────────────────────────────────────────────

class FakeRiskManager:
    class state:
        kill_switch_triggered = False

    def check_directional_order(
        self, order, open_count, directional_exposure, max_position, max_total, max_open
    ):
        return True


# ── Config ───────────────────────────────────────────────────────────────────

def _make_config():
    sc = SimpleNamespace(mode="paper", min_edge_cents=1, skip_categories=[])
    ai = SimpleNamespace(
        mode="paper",
        min_confidence=0.70,
        min_edge_pct=0.05,
        kelly_fraction=0.25,
        stop_loss_pct=0.30,
        take_profit_pct=0.50,
        max_hold_hours=72.0,
    )
    caps = SimpleNamespace(total_exposure=30.0, max_position=8.0, max_open=4)
    return SimpleNamespace(
        db_path=":memory:",
        scan_interval_seconds=0,
        markets_per_cycle=50,
        category_exclude=[],
        min_volume=100,
        caps=caps,
        safe_compounder=sc,
        ai_directional=ai,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_paper_run_records_two_positions():
    """run_once records 1 SafeCompounder + 1 AiDirectional position in paper mode."""
    market_a = _market("KX-CHEAP-NO", yes_price=0.05, no_price=0.93, vol=1000)
    market_b = _market("KX-AI-YES", yes_price=0.55, no_price=0.44, vol=1000)

    client = FakeKalshiClient(
        markets=[market_a, market_b],
        no_asks={"KX-CHEAP-NO": 0.07, "KX-AI-YES": 0.45},
    )
    # No markets resolved during run_once
    cfg = _make_config()
    engine = DirectionalEngine(cfg, client, FakeIntel(), FakeRiskManager())

    await engine.run_once()

    positions = engine.store.open_positions()
    assert len(positions) == 2, f"Expected 2 positions, got {len(positions)}: {positions}"

    strategies = {p.strategy for p in positions}
    assert "safe_compounder" in strategies, f"safe_compounder missing from {strategies}"
    assert "ai_directional" in strategies, f"ai_directional missing from {strategies}"

    for p in positions:
        assert p.mode == "paper"
        assert p.status == "open"

    # place_order must NOT be called in paper mode
    assert client.place_order_calls == [], "place_order must not be called in paper mode"


@pytest.mark.asyncio
async def test_tracker_sweep_settles_resolved_market():
    """After run_once (no resolution), set Market B resolved=YES; sweep closes it with positive P&L."""
    market_a = _market("KX-CHEAP-NO", yes_price=0.05, no_price=0.93, vol=1000)
    market_b = _market("KX-AI-YES", yes_price=0.55, no_price=0.44, vol=1000)

    client = FakeKalshiClient(
        markets=[market_a, market_b],
        no_asks={"KX-CHEAP-NO": 0.07, "KX-AI-YES": 0.45},
    )
    cfg = _make_config()
    engine = DirectionalEngine(cfg, client, FakeIntel(), FakeRiskManager())

    # Phase 1: run_once with no markets resolved → both positions open
    await engine.run_once()

    positions_before = engine.store.open_positions()
    assert len(positions_before) == 2, (
        f"Expected 2 open positions before sweep, got {len(positions_before)}"
    )

    # Confirm the AI position is on the YES side
    ai_pos = next(p for p in positions_before if p.strategy == "ai_directional")
    assert ai_pos.side == "YES"

    # Phase 2: Market B resolves YES — set AFTER run_once so the tracker
    # inside run_once didn't already close it.
    client.resolved["KX-AI-YES"] = "yes"

    # Explicit sweep call: should detect Market B resolved YES and close AI position
    await engine.tracker.sweep(now=datetime.utcnow())

    positions_after = engine.store.open_positions()
    assert len(positions_after) == 1, (
        f"Expected 1 open position after sweep, got {len(positions_after)}"
    )
    remaining = positions_after[0]
    assert remaining.strategy == "safe_compounder"

    # The closed position should have positive P&L: bought YES at ~0.55, resolved at 1.0
    summary = engine.store.pnl_summary()
    assert summary["closed_count"] == 1
    assert summary["total_realized_pnl"] > 0, (
        f"Expected positive P&L for YES position resolved YES, got {summary['total_realized_pnl']}"
    )


@pytest.mark.asyncio
async def test_no_live_api_calls_during_paper_run():
    """The entire integration (run_once + sweep) never calls place_order."""
    market_a = _market("KX-CHEAP-NO", yes_price=0.05, no_price=0.93, vol=1000)
    market_b = _market("KX-AI-YES", yes_price=0.55, no_price=0.44, vol=1000)

    client = FakeKalshiClient(
        markets=[market_a, market_b],
        no_asks={"KX-CHEAP-NO": 0.07, "KX-AI-YES": 0.45},
    )
    cfg = _make_config()
    engine = DirectionalEngine(cfg, client, FakeIntel(), FakeRiskManager())

    await engine.run_once()
    await engine.tracker.sweep(now=datetime.utcnow())

    assert client.place_order_calls == [], (
        f"place_order was called unexpectedly: {client.place_order_calls}"
    )


@pytest.mark.asyncio
async def test_safe_compounder_no_side_tracked():
    """SafeCompounder position is always on the NO side."""
    market_a = _market("KX-CHEAP-NO", yes_price=0.05, no_price=0.93, vol=1000)
    client = FakeKalshiClient(
        markets=[market_a],
        no_asks={"KX-CHEAP-NO": 0.07},
    )
    cfg = _make_config()
    # Disable AI by passing None as intelligence_engine
    engine = DirectionalEngine(cfg, client, None, FakeRiskManager())

    await engine.run_once()

    positions = engine.store.open_positions()
    assert len(positions) == 1
    assert positions[0].side == "NO"
    assert positions[0].strategy == "safe_compounder"
