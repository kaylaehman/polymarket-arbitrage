"""Tests for core/directional/engine.py — Task 14.

TDD: write failing tests first, then implement engine.py.

Key assertions:
- run_once() in paper mode records positions without calling place_order live.
- Engine wires scanner, strategies, decider, executor, tracker correctly.
- run_forever() catches exceptions and continues (does not propagate).
"""
import asyncio
import pytest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.directional.store import DirectionalStore
from core.directional.models import DirectionalCandidate, DirectionalPosition


# ── Fake KalshiMarket ────────────────────────────────────────────────────────

def make_market(ticker="KX-TEST-1", yes_price=0.05, no_price=0.93, volume=500):
    """Build a minimal fake KalshiMarket."""
    m = SimpleNamespace()
    m.ticker = ticker
    m.event_ticker = ticker
    m.title = f"Test market {ticker}"
    m.yes_price = yes_price
    m.no_price = no_price
    m.volume = volume
    m.category = "Finance"
    m.status = "open"
    m.result = None
    m.to_unified_market_id = lambda: f"kalshi:{ticker}"
    return m


# ── Fake kalshi_client ───────────────────────────────────────────────────────

class FakeKalshiClient:
    """Minimal fake that never hits a live API."""

    def __init__(self, markets=None, no_ask=0.07):
        self._markets = markets or [make_market()]
        self._no_ask = no_ask
        self.place_order_calls = []

    async def _get(self, endpoint, params=None):
        """Support scanner's /events fetch: return all markets as nested events."""
        nested = [
            {
                "ticker": m.ticker,
                "event_ticker": m.event_ticker,
                "series_ticker": getattr(m, "series_ticker", m.event_ticker),
                "title": m.title,
                "status": "open",
                "close_time": None,
                "volume": None,
                "yes_price": None,
                "no_price": None,
            }
            for m in self._markets
        ]
        return {"events": [{"markets": nested, "event_ticker": "FAKE"}], "cursor": None}

    async def list_all_markets(self, status="open", max_markets=50):
        return self._markets

    async def get_orderbook_unified(self, ticker):
        # Return a minimal fake orderbook with a tight YES spread (≤MAX_SPREAD)
        # centred near the market's yes_price so SafeCompounder gets correct edge.
        market_yes = next(
            (m.yes_price for m in self._markets if m.ticker == ticker), 0.05
        )
        ob = SimpleNamespace()
        yes_side = SimpleNamespace()
        yes_side.best_bid = round(market_yes - 0.01, 4)
        yes_side.best_ask = round(market_yes + 0.01, 4)
        yes_side.mid_price = market_yes
        no_side = SimpleNamespace()
        no_side.best_ask = self._no_ask
        no_side.best_bid = self._no_ask - 0.01
        no_side.mid_price = self._no_ask - 0.005
        ob.yes = yes_side
        ob.no = no_side
        return ob

    async def get_market(self, ticker):
        for m in self._markets:
            if m.ticker == ticker or m.ticker in ticker:
                return m
        return None

    async def place_order(self, **kwargs):
        self.place_order_calls.append(kwargs)
        return object()

    async def get_balance(self):
        return 100.0


# ── Fake intelligence engine ─────────────────────────────────────────────────

class FakeIntelligenceEngine:
    """Returns None — AI strategy emits no candidates."""

    async def evaluate(self, **kwargs):
        return None


# ── Fake risk manager ────────────────────────────────────────────────────────

class FakeRiskManager:
    class state:
        kill_switch_triggered = False

    def check_directional_order(
        self, order, open_count, directional_exposure, max_position, max_total, max_open
    ):
        return True  # always approve in tests


# ── Fake config ──────────────────────────────────────────────────────────────

def make_config(sc_min_edge=3, ai_min_confidence=0.7, ai_min_edge=0.05):
    """Build a minimal directional config namespace."""
    sc = SimpleNamespace(
        mode="paper",
        min_edge_cents=sc_min_edge,
        skip_categories=[],
    )
    ai = SimpleNamespace(
        mode="paper",
        min_confidence=ai_min_confidence,
        min_edge_pct=ai_min_edge,
        kelly_fraction=0.25,
        stop_loss_pct=0.30,
        take_profit_pct=0.50,
        max_hold_hours=72.0,
    )
    caps = SimpleNamespace(
        total_exposure=30.0,
        max_position=8.0,
        max_open=4,
    )
    return SimpleNamespace(
        db_path=":memory:",
        scan_interval_seconds=60,
        markets_per_cycle=50,
        category_exclude=[],
        min_volume=100,
        caps=caps,
        safe_compounder=sc,
        ai_directional=ai,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_paper_records_intended(tmp_path):
    """run_once with SafeCompounder paper mode records a position; place_order NOT called."""
    from core.directional.engine import DirectionalEngine

    cfg = make_config(sc_min_edge=1)  # low threshold to ensure candidate passes
    # Market: yes_price=0.05 → fair_no=0.95; no_ask=0.07; edge=0.88 > 0.01
    client = FakeKalshiClient(
        markets=[make_market(yes_price=0.05, no_price=0.93, volume=500)],
        no_ask=0.07,
    )
    engine = DirectionalEngine(cfg, client, FakeIntelligenceEngine(), FakeRiskManager())

    await engine.run_once()

    positions = engine.store.open_positions()
    assert len(positions) == 1, f"Expected 1 position, got {len(positions)}"
    assert positions[0].mode == "paper"
    assert positions[0].strategy == "safe_compounder"
    assert client.place_order_calls == [], "place_order must NOT be called in paper mode"


@pytest.mark.asyncio
async def test_run_once_exposes_store():
    """Engine exposes .store attribute."""
    from core.directional.engine import DirectionalEngine

    cfg = make_config()
    engine = DirectionalEngine(cfg, FakeKalshiClient(), FakeIntelligenceEngine(), FakeRiskManager())
    assert engine.store is not None
    assert hasattr(engine.store, "open_positions")


@pytest.mark.asyncio
async def test_run_once_calls_tracker_sweep():
    """After scanning/deciding, run_once calls tracker.sweep."""
    from core.directional.engine import DirectionalEngine

    cfg = make_config()
    client = FakeKalshiClient(markets=[])  # empty so no candidates
    engine = DirectionalEngine(cfg, client, FakeIntelligenceEngine(), FakeRiskManager())

    swept = []
    original_sweep = engine.tracker.sweep

    async def fake_sweep(*a, **kw):
        swept.append(True)
        return await original_sweep(*a, **kw)

    engine.tracker.sweep = fake_sweep
    await engine.run_once()
    assert swept, "tracker.sweep must be called by run_once"


@pytest.mark.asyncio
async def test_run_forever_survives_exception():
    """run_forever catches exceptions from run_once and continues rather than propagating."""
    from core.directional.engine import DirectionalEngine

    cfg = make_config()
    cfg.scan_interval_seconds = 0  # no sleep between iterations
    engine = DirectionalEngine(cfg, FakeKalshiClient(markets=[]), FakeIntelligenceEngine(), FakeRiskManager())

    call_count = 0

    async def bad_run_once():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("simulated crash")
        # After 3 calls stop the loop
        engine._running = False

    engine.run_once = bad_run_once

    # run_forever should not propagate the exception
    engine._running = True
    await engine.run_forever()
    assert call_count >= 3, "run_forever should keep running after exception"


@pytest.mark.asyncio
async def test_engine_maker_uses_last_liquid_not_capped_list():
    """Regression test: MakerLongshotStrategy receives scanner.last_liquid (full pre-cap
    set) so near-term longshots at high spread-sort indexes are not silently dropped.

    Setup: 20 markets where 19 have tight spreads (beat KXNEAR-LONGSHOT in sort).
    With markets_per_cycle=15, scan(15) excludes KXNEAR-LONGSHOT.
    The engine must still route the longshot to the maker via last_liquid.
    """
    from datetime import datetime, timezone, timedelta
    from core.directional.engine import DirectionalEngine

    # Shared close_time builder
    def make_close(days):
        return datetime.now(timezone.utc) + timedelta(days=days)

    # 19 tight-spread far markets that monopolise the top-15 capped list
    tight_markets = []
    for i in range(19):
        m = make_market(ticker=f"KXTIGHT-{i:02d}", yes_price=0.50)
        m.event_ticker = f"KXTIGHT-{i:02d}"
        m.close_time = make_close(500)
        tight_markets.append(m)

    # 1 near-term longshot: yes=0.08, spread wider than tight markets
    longshot = make_market(ticker="KXNEAR-LONGSHOT", yes_price=0.08)
    longshot.event_ticker = "KXNEAR-LONGSHOT"
    longshot.close_time = make_close(10)
    longshot.category = "Sports"

    all_markets = tight_markets + [longshot]

    # FakeKalshiClient extended to return per-market orderbooks with correct spreads
    class ExtendedFakeClient(FakeKalshiClient):
        def __init__(self, markets, longshot_ticker, longshot_no_ask=0.94):
            super().__init__(markets, no_ask=0.52)
            self._longshot = longshot_ticker
            self._longshot_no_ask = longshot_no_ask

        async def _get(self, endpoint, params=None):
            # serialize close_time for the scanner
            def _ct(m):
                ct = getattr(m, "close_time", None)
                return ct.strftime("%Y-%m-%dT%H:%M:%S+00:00") if ct else None

            nested = [
                {
                    "ticker": m.ticker,
                    "event_ticker": getattr(m, "event_ticker", m.ticker),
                    "series_ticker": getattr(m, "series_ticker", m.ticker),
                    "title": m.title,
                    "status": "open",
                    "close_time": _ct(m),
                }
                for m in self._markets
            ]
            return {"events": [{"markets": nested, "event_ticker": "FAKE"}], "cursor": None}

        async def get_orderbook_unified(self, ticker):
            from types import SimpleNamespace
            ob = SimpleNamespace()
            if ticker == self._longshot:
                # wider spread = 0.05; yes_mid=0.075 → yes band [0.05, 0.20]
                yes = SimpleNamespace(best_bid=0.05, best_ask=0.10)
                no = SimpleNamespace(best_ask=self._longshot_no_ask, best_bid=self._longshot_no_ask - 0.01)
            else:
                # tight spread = 0.02; yes_mid=0.50 → outside longshot band
                yes = SimpleNamespace(best_bid=0.49, best_ask=0.51)
                no = SimpleNamespace(best_ask=0.52, best_bid=0.51)
            ob.yes = yes
            ob.no = no
            return ob

    client = ExtendedFakeClient(all_markets, "KXNEAR-LONGSHOT", longshot_no_ask=0.94)

    # Config: markets_per_cycle=15 (production default), maker enabled
    ml = SimpleNamespace(
        mode="paper",
        min_structural_score=0.02,
        min_yes_price=0.05,
        max_yes_price=0.20,
        price_improvement_cents=1,
        order_ttl_minutes=60.0,
        skip_categories=[],
        max_days_to_resolution=30.0,
    )
    cfg = make_config()
    cfg.markets_per_cycle = 15
    cfg.maker_longshot = ml

    engine = DirectionalEngine(cfg, client, FakeIntelligenceEngine(), FakeRiskManager())
    await engine.run_once()

    positions = engine.store.open_positions()
    maker_positions = [p for p in positions if p.strategy == "maker_longshot"]
    assert len(maker_positions) >= 1, (
        f"Expected at least 1 maker_longshot position for near-term longshot; "
        f"got positions: {[(p.strategy, p.market_id) for p in positions]}"
    )
    assert maker_positions[0].market_id == "kalshi:KXNEAR-LONGSHOT"


# ── Dedup tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_once_skips_duplicate_market_same_strategy():
    """run_once must NOT create a second position when the store already holds an
    open maker_longshot position for that market_id.

    Regression guard for the bug where every scan cycle stacked a new identical
    position for KXCABLEAVE-26MAY22-26JUL until the $30 total-exposure cap hit.
    """
    from datetime import datetime, timezone, timedelta
    from core.directional.engine import DirectionalEngine
    from core.directional.models import DirectionalPosition

    # Market that already has an open maker_longshot position in the store
    DUP_TICKER = "KXCABLEAVE-26MAY22-26JUL"
    DUP_MARKET_ID = f"kalshi:{DUP_TICKER}"

    # Fresh market that has NO prior position — should still be placed
    NEW_TICKER = "KXNEWMARKET-FRESH"
    NEW_MARKET_ID = f"kalshi:{NEW_TICKER}"

    def make_close(days):
        return datetime.now(timezone.utc) + timedelta(days=days)

    dup_market = make_market(ticker=DUP_TICKER, yes_price=0.06)
    dup_market.event_ticker = DUP_TICKER
    dup_market.close_time = make_close(35)

    new_market = make_market(ticker=NEW_TICKER, yes_price=0.07)
    new_market.event_ticker = NEW_TICKER
    new_market.close_time = make_close(20)

    all_markets = [dup_market, new_market]

    class DedupeTestClient(FakeKalshiClient):
        async def _get(self, endpoint, params=None):
            def _ct(m):
                ct = getattr(m, "close_time", None)
                return ct.strftime("%Y-%m-%dT%H:%M:%S+00:00") if ct else None

            nested = [
                {
                    "ticker": m.ticker,
                    "event_ticker": getattr(m, "event_ticker", m.ticker),
                    "series_ticker": getattr(m, "series_ticker", m.ticker),
                    "title": m.title,
                    "status": "open",
                    "close_time": _ct(m),
                }
                for m in self._markets
            ]
            return {"events": [{"markets": nested, "event_ticker": "FAKE"}], "cursor": None}

        async def get_orderbook_unified(self, ticker):
            ob = SimpleNamespace()
            # Both markets qualify as maker_longshot candidates
            yes = SimpleNamespace(best_bid=0.05, best_ask=0.09)
            no = SimpleNamespace(best_ask=0.94, best_bid=0.93)
            ob.yes = yes
            ob.no = no
            return ob

    client = DedupeTestClient(all_markets, no_ask=0.94)

    ml = SimpleNamespace(
        mode="paper",
        min_structural_score=0.02,
        min_yes_price=0.05,
        max_yes_price=0.20,
        price_improvement_cents=1,
        order_ttl_minutes=60.0,
        skip_categories=[],
        max_days_to_resolution=60.0,
    )
    cfg = make_config()
    cfg.maker_longshot = ml
    # Disable other strategies to isolate maker_longshot behaviour
    cfg.safe_compounder = None
    cfg.ai_directional = None

    engine = DirectionalEngine(cfg, client, FakeIntelligenceEngine(), FakeRiskManager())

    # Pre-seed the store with an existing open maker_longshot position for DUP_TICKER
    existing = DirectionalPosition(
        market_id=DUP_MARKET_ID,
        side="NO",
        entry_price=0.94,
        size=8,
        strategy="maker_longshot",
        mode="paper",
        opened_at=datetime.now(timezone.utc),
        stop_loss=None,
        take_profit=None,
        notional=7.52,
        status="open",
        order_id=None,
    )
    engine.store.record_position(existing)

    # Run one scan cycle
    await engine.run_once()

    open_positions = engine.store.open_positions()

    # Count positions per market_id
    dup_positions = [p for p in open_positions if p.market_id == DUP_MARKET_ID]
    new_positions = [p for p in open_positions if p.market_id == NEW_MARKET_ID]

    assert len(dup_positions) == 1, (
        f"Expected exactly 1 position for {DUP_MARKET_ID} (dedup must prevent stacking); "
        f"got {len(dup_positions)}"
    )
    assert len(new_positions) >= 1, (
        f"Expected at least 1 position for fresh market {NEW_MARKET_ID} (dedup must not block new markets); "
        f"got {len(new_positions)}"
    )


@pytest.mark.asyncio
async def test_run_once_skips_pending_market_same_strategy():
    """run_once must also skip a market that has a PENDING (not just open) position."""
    from datetime import datetime, timezone, timedelta
    from core.directional.engine import DirectionalEngine
    from core.directional.models import DirectionalPosition

    PENDING_TICKER = "KXPENDING-MARKET"
    PENDING_MARKET_ID = f"kalshi:{PENDING_TICKER}"

    def make_close(days):
        return datetime.now(timezone.utc) + timedelta(days=days)

    pending_market = make_market(ticker=PENDING_TICKER, yes_price=0.06)
    pending_market.event_ticker = PENDING_TICKER
    pending_market.close_time = make_close(25)

    class PendingDedupeClient(FakeKalshiClient):
        async def _get(self, endpoint, params=None):
            def _ct(m):
                ct = getattr(m, "close_time", None)
                return ct.strftime("%Y-%m-%dT%H:%M:%S+00:00") if ct else None

            nested = [
                {
                    "ticker": m.ticker,
                    "event_ticker": getattr(m, "event_ticker", m.ticker),
                    "series_ticker": getattr(m, "series_ticker", m.ticker),
                    "title": m.title,
                    "status": "open",
                    "close_time": _ct(m),
                }
                for m in self._markets
            ]
            return {"events": [{"markets": nested, "event_ticker": "FAKE"}], "cursor": None}

        async def get_orderbook_unified(self, ticker):
            ob = SimpleNamespace()
            yes = SimpleNamespace(best_bid=0.05, best_ask=0.09)
            no = SimpleNamespace(best_ask=0.94, best_bid=0.93)
            ob.yes = yes
            ob.no = no
            return ob

    client = PendingDedupeClient([pending_market], no_ask=0.94)

    ml = SimpleNamespace(
        mode="paper",
        min_structural_score=0.02,
        min_yes_price=0.05,
        max_yes_price=0.20,
        price_improvement_cents=1,
        order_ttl_minutes=60.0,
        skip_categories=[],
        max_days_to_resolution=60.0,
    )
    cfg = make_config()
    cfg.maker_longshot = ml
    cfg.safe_compounder = None
    cfg.ai_directional = None

    engine = DirectionalEngine(cfg, client, FakeIntelligenceEngine(), FakeRiskManager())

    # Pre-seed with a PENDING position (live maker, awaiting fill)
    pending_pos = DirectionalPosition(
        market_id=PENDING_MARKET_ID,
        side="NO",
        entry_price=0.94,
        size=8,
        strategy="maker_longshot",
        mode="live",
        opened_at=datetime.now(timezone.utc),
        stop_loss=None,
        take_profit=None,
        notional=7.52,
        status="pending",
        order_id="test-order-123",
    )
    engine.store.record_position(pending_pos)

    await engine.run_once()

    # Only the pre-seeded pending position should exist; no new open position
    all_active = engine.store.open_positions() + engine.store.pending_positions()
    for_market = [p for p in all_active if p.market_id == PENDING_MARKET_ID]
    assert len(for_market) == 1, (
        f"Expected exactly 1 active position for {PENDING_MARKET_ID} (pending dedup); "
        f"got {len(for_market)}: {[(p.status, p.market_id) for p in for_market]}"
    )
