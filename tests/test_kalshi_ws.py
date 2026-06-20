"""
Tests for Kalshi WebSocket book maintenance and config.
Tasks 1 and 2 of the websocket-feeds implementation plan.
"""

import pytest
from utils.config_loader import MonitoringConfig


# ── Task 1: Config defaults ───────────────────────────────────────────────────

def test_ws_config_defaults():
    """MonitoringConfig must have the three WS fields with correct defaults."""
    cfg = MonitoringConfig()
    assert cfg.kalshi_ws_enabled is True
    assert cfg.ws_staleness_seconds == 10.0
    assert cfg.ws_reconcile_seconds == 120.0


# ── Task 2: Book maintenance (pure — no socket) ───────────────────────────────

from kalshi_client.ws import _BookState, apply_snapshot, apply_delta, book_to_orderbook


def _snapshot_msg(yes_levels, no_levels, ticker="TICKER-A"):
    """Build a synthetic orderbook_snapshot msg dict."""
    return {
        "market_ticker": ticker,
        "market_id": "mid-123",
        "yes_dollars_fp": [[str(p), str(s)] for p, s in yes_levels],
        "no_dollars_fp":  [[str(p), str(s)] for p, s in no_levels],
    }


def _delta_msg(side, price, delta, ticker="TICKER-A"):
    """Build a synthetic orderbook_delta msg dict."""
    return {
        "market_ticker": ticker,
        "price_dollars": str(price),
        "delta_fp": str(delta),
        "side": side,
        "ts_ms": 1_700_000_000_000,
    }


class TestApplySnapshot:
    def test_populates_yes_and_no(self):
        state = _BookState()
        msg = _snapshot_msg([(0.60, 100.0)], [(0.35, 200.0)])
        apply_snapshot(state, msg)
        assert state.yes == {0.60: 100.0}
        assert state.no == {0.35: 200.0}

    def test_replaces_existing_data(self):
        state = _BookState(yes={0.99: 999.0}, no={0.01: 888.0})
        msg = _snapshot_msg([(0.50, 50.0)], [(0.45, 60.0)])
        apply_snapshot(state, msg)
        assert state.yes == {0.50: 50.0}
        assert state.no == {0.45: 60.0}

    def test_empty_snapshot_clears(self):
        state = _BookState(yes={0.5: 100.0}, no={0.4: 100.0})
        apply_snapshot(state, _snapshot_msg([], []))
        assert state.yes == {}
        assert state.no == {}


class TestApplyDelta:
    def test_adds_new_level(self):
        state = _BookState()
        apply_delta(state, _delta_msg("yes", 0.60, 500.0))
        assert state.yes[0.60] == pytest.approx(500.0)

    def test_cumulates_positive_delta(self):
        state = _BookState(yes={0.60: 1000.0})
        apply_delta(state, _delta_msg("yes", 0.60, 500.0))
        assert state.yes[0.60] == pytest.approx(1500.0)

    def test_cumulates_negative_delta(self):
        state = _BookState(yes={0.60: 1000.0})
        apply_delta(state, _delta_msg("yes", 0.60, -300.0))
        assert state.yes[0.60] == pytest.approx(700.0)

    def test_removes_level_at_zero(self):
        state = _BookState(yes={0.60: 500.0})
        apply_delta(state, _delta_msg("yes", 0.60, -500.0))
        assert 0.60 not in state.yes

    def test_removes_level_below_zero(self):
        state = _BookState(yes={0.60: 500.0})
        apply_delta(state, _delta_msg("yes", 0.60, -600.0))
        assert 0.60 not in state.yes

    def test_no_side_delta(self):
        state = _BookState()
        apply_delta(state, _delta_msg("no", 0.35, 200.0))
        assert state.no[0.35] == pytest.approx(200.0)


class TestBookToOrderbook:
    def test_snapshot_then_delta_best_bid_ask(self):
        """After snapshot + delta, best_bid_yes and best_ask_yes are correct."""
        state = _BookState()
        # YES bids: 0.60 x 1000, 0.55 x 500
        # NO bids:  0.35 x 800,  0.30 x 400
        apply_snapshot(state, _snapshot_msg(
            [(0.60, 1000.0), (0.55, 500.0)],
            [(0.35, 800.0),  (0.30, 400.0)],
        ))
        # Add more YES size at 0.60
        apply_delta(state, _delta_msg("yes", 0.60, 200.0))

        ob = book_to_orderbook("TICKER-A", state)
        # best bid YES = highest YES bid = 0.60
        assert ob.best_bid_yes == pytest.approx(0.60)
        # best ask YES = 1 - best NO bid = 1 - 0.35 = 0.65
        assert ob.best_ask_yes == pytest.approx(0.65)

    def test_delta_removes_level(self):
        """A level driven to <=0 must disappear from the book."""
        state = _BookState()
        apply_snapshot(state, _snapshot_msg(
            [(0.60, 500.0), (0.55, 300.0)],
            [(0.35, 200.0)],
        ))
        # Wipe the 0.60 level completely
        apply_delta(state, _delta_msg("yes", 0.60, -500.0))

        ob = book_to_orderbook("TICKER-A", state)
        # The 0.60 bid should be gone; best bid is now 0.55
        assert ob.best_bid_yes == pytest.approx(0.55)

    def test_multi_level_best_ask_is_lowest_derived(self):
        """
        With multiple NO bids, the derived YES asks must be sorted ASCENDING
        so best_ask_yes == the LOWEST derived ask (i.e. 1 - highest NO bid).
        """
        state = _BookState()
        # NO bids: 0.40 x 500, 0.35 x 300, 0.30 x 200
        apply_snapshot(state, _snapshot_msg(
            [(0.55, 400.0)],
            [(0.40, 500.0), (0.35, 300.0), (0.30, 200.0)],
        ))
        ob = book_to_orderbook("TICKER-A", state)

        # YES asks are derived as (1 - no_bid_price):
        #   1 - 0.40 = 0.60  <- lowest ask (best)
        #   1 - 0.35 = 0.65
        #   1 - 0.30 = 0.70
        assert ob.best_ask_yes == pytest.approx(0.60)

    def test_market_id_format(self):
        """book_to_orderbook must prefix market_id with 'kalshi:'."""
        state = _BookState()
        apply_snapshot(state, _snapshot_msg([(0.5, 100.0)], [(0.45, 100.0)]))
        ob = book_to_orderbook("SOME-TICKER", state)
        assert ob.market_id == "kalshi:SOME-TICKER"


# ── Task 3: KalshiWSClient (connect/subscribe/reconnect) ─────────────────────

import asyncio
import json

from kalshi_client.ws import KalshiWSClient


class FakeWS:
    """Minimal fake WebSocket: pops messages from a list, raises CancelledError when empty."""

    def __init__(self, messages: list):
        self._msgs = list(messages)
        self.sent: list = []

    async def send(self, m: str) -> None:
        self.sent.append(json.loads(m))

    async def recv(self) -> str:
        if self._msgs:
            return self._msgs.pop(0)
        raise asyncio.CancelledError

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeKalshi:
    def _auth_headers(self, method: str, path: str) -> dict:
        return {}


def _ws_snapshot(ticker: str, seq: int, yes_levels=None, no_levels=None) -> str:
    """Build a full outer WS envelope with an orderbook_snapshot inner msg."""
    yes_levels = yes_levels or [[0.40, 100]]
    no_levels = no_levels or [[0.55, 200]]
    return json.dumps({
        "type": "orderbook_snapshot",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[str(p), str(s)] for p, s in yes_levels],
            "no_dollars_fp": [[str(p), str(s)] for p, s in no_levels],
        },
    })


def _ws_delta(ticker: str, seq: int, side: str, price: float, delta: float) -> str:
    """Build a full outer WS envelope with an orderbook_delta inner msg."""
    return json.dumps({
        "type": "orderbook_delta",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": str(price),
            "delta_fp": str(delta),
        },
    })


async def test_ws_client_subscribes_and_routes_updates():
    """Client sends a subscribe command and routes snapshot to on_book_update."""
    snap = _ws_snapshot("KX-1", seq=1, yes_levels=[[0.40, 100]], no_levels=[[0.55, 200]])
    fake = FakeWS([snap])
    updates = []

    async def on_update(t, ob):
        updates.append(t)

    c = KalshiWSClient(
        _FakeKalshi(),
        on_book_update=on_update,
        connect_fn=lambda url, additional_headers=None: fake,
    )
    try:
        await asyncio.wait_for(c.run(["KX-1"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert fake.sent, "expected at least one sent message"
    assert fake.sent[0]["cmd"] == "subscribe"
    assert "KX-1" in fake.sent[0]["params"]["market_tickers"]
    assert updates == ["KX-1"]
    assert c.books.get("KX-1") is not None


async def test_seq_gap_drops_book():
    """A delta with seq != last_seq+1 drops the book state; no update emitted.

    Prevents phantom arb signals on live money: a gap means we missed events,
    so the locally-maintained book is stale. Drop it and wait for a fresh snapshot.
    """
    snap = _ws_snapshot("KX-1", seq=1, yes_levels=[[0.40, 100]], no_levels=[[0.55, 200]])
    # seq=3 but last_seq was 1 → gap (expected 2)
    bad_delta = _ws_delta("KX-1", seq=3, side="yes", price=0.40, delta=-100)
    fake = FakeWS([snap, bad_delta])
    updates = []

    async def on_update(t, ob):
        updates.append((t, ob))

    c = KalshiWSClient(
        _FakeKalshi(),
        on_book_update=on_update,
        connect_fn=lambda url, additional_headers=None: fake,
    )
    try:
        await asyncio.wait_for(c.run(["KX-1"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # Snapshot fires one update; bad delta must NOT fire a second update
    assert len(updates) == 1
    # After gap the book is dropped: best_ask_yes is None
    assert c.books.get("KX-1") is None or c.books["KX-1"].best_ask_yes is None


async def test_envelope_unwrap_updates_book():
    """Feed a full envelope; assert the book updates correctly (proves unwrap works)."""
    snap = _ws_snapshot("KX-2", seq=1, yes_levels=[[0.60, 50]], no_levels=[[0.35, 80]])
    fake = FakeWS([snap])
    updates = []

    async def on_update(t, ob):
        updates.append((t, ob))

    c = KalshiWSClient(
        _FakeKalshi(),
        on_book_update=on_update,
        connect_fn=lambda url, additional_headers=None: fake,
    )
    try:
        await asyncio.wait_for(c.run(["KX-2"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert updates, "expected at least one book update"
    ticker, ob = updates[0]
    assert ticker == "KX-2"
    # no bid 0.35 → yes ask = 1 - 0.35 = 0.65
    assert round(ob.best_ask_yes, 2) == 0.65


async def test_reconnect_after_one_failure():
    """connect_fn raises once then returns a fake with a snapshot; update is routed."""
    snap = _ws_snapshot("KX-3", seq=1, yes_levels=[[0.55, 100]], no_levels=[[0.40, 150]])

    attempt = [0]
    fake = FakeWS([snap])

    def fake_connect(url, additional_headers=None):
        attempt[0] += 1
        if attempt[0] == 1:
            raise OSError("simulated connection failure")
        return fake

    updates = []

    async def on_update(t, ob):
        updates.append(t)

    # inject sleep_fn so the exponential backoff doesn't actually wait
    async def no_sleep(s):
        pass

    c = KalshiWSClient(
        _FakeKalshi(),
        on_book_update=on_update,
        connect_fn=fake_connect,
        sleep_fn=no_sleep,
    )
    try:
        await asyncio.wait_for(c.run(["KX-3"]), timeout=2)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    assert attempt[0] >= 2, "expected at least two connection attempts"
    assert updates == ["KX-3"]


# ── Task 7: Integration tests (Tasks 6+7 cross-path dedup) ───────────────────

import json as _json

from core.arb_engine import ArbEngine, ArbConfig
from core.kalshi_ws_detector import WSBundleDetector
from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


def _make_arb_orderbook(market_id: str, total_ask: float = 0.75, size: float = 500.0) -> OrderBook:
    """Build an OrderBook where best_ask_yes + best_ask_no == total_ask.

    Splits the total_ask evenly across YES and NO ask sides.
    Also populates bids so ArbEngine sizing logic has non-None values.
    """
    half = total_ask / 2.0
    yes_ask = half
    no_ask = half
    yes_bid = yes_ask - 0.05
    no_bid = no_ask - 0.05
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=[PriceLevel(price=yes_bid, size=size)]),
            asks=OrderBookSide(levels=[PriceLevel(price=yes_ask, size=size)]),
        ),
        no=TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=[PriceLevel(price=no_bid, size=size)]),
            asks=OrderBookSide(levels=[PriceLevel(price=no_ask, size=size)]),
        ),
    )


def _make_ws_snapshot_msg(ticker: str, yes_bid: float, no_bid: float, seq: int = 1) -> str:
    """Build a WS envelope with an orderbook_snapshot whose book will have
    best_ask_yes = 1 - no_bid and best_ask_no = 1 - yes_bid (WS derivation)."""
    return _json.dumps({
        "type": "orderbook_snapshot",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[str(yes_bid), "500.0"]],
            "no_dollars_fp": [[str(no_bid), "500.0"]],
        },
    })


def _make_ws_delta_msg(ticker: str, side: str, price: float, delta: float, seq: int = 2) -> str:
    """Build a WS envelope with an orderbook_delta."""
    return _json.dumps({
        "type": "orderbook_delta",
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": str(price),
            "delta_fp": str(delta),
        },
    })


class _FakeArbEngine:
    """Thin fake arb engine: returns a signal when total_ask < 1.00.

    Uses a real Signal-like stub so WSBundleDetector can iterate over it.
    """

    def __init__(self):
        self.call_count = 0

    def analyze(self, market_state: MarketState):
        self.call_count += 1
        ob = market_state.order_book
        if ob.best_ask_yes is not None and ob.best_ask_no is not None:
            if ob.best_ask_yes + ob.best_ask_no < 1.00:
                return [_FakeSignal(market_state.market.market_id)]
        return []


class _FakeSignal:
    """Minimal signal stub — only needs market_id; opportunity is None (safe to skip)."""

    def __init__(self, market_id: str):
        self.market_id = market_id
        self.opportunity = None


class _FakeExecEngine:
    """Async stub execution engine that records submitted signals."""

    def __init__(self):
        self.submitted: list = []

    async def submit_signal(self, signal) -> None:
        self.submitted.append(signal)


async def test_e2e_snapshot_delta_routes_one_signal():
    """Drive KalshiWSClient (fake transport) with snapshot+delta that produces a
    book where best_ask_yes + best_ask_no < 1.00; assert exactly ONE signal submitted.

    The WS-derived orderbook has:
        best_ask_yes = 1 - no_bid
        best_ask_no  = 1 - yes_bid
    Starting snapshot: yes_bid=0.55, no_bid=0.40 → total_ask=1.05 (no signal)
    After delta: yes_bid increases to 0.65 → best_ask_no = 0.35, total_ask = 0.95 < 1.00 (signal)
    """
    ticker = "TASK7-A"
    # Snapshot: yes_bid=0.55, no_bid=0.40 → best_ask_yes=0.60, best_ask_no=0.45, total=1.05
    snap = _make_ws_snapshot_msg(ticker, yes_bid=0.55, no_bid=0.40, seq=1)
    # Delta: add size to yes bid at 0.65 (new best yes bid)
    # → best_ask_no = 1 - 0.65 = 0.35, total_ask = 0.60 + 0.35 = 0.95 < 1.00
    delta = _make_ws_delta_msg(ticker, side="yes", price=0.65, delta=300.0, seq=2)

    fake_ws = FakeWS([snap, delta])
    exec_engine = _FakeExecEngine()
    fake_arb = _FakeArbEngine()

    detector = WSBundleDetector(
        arb_engine=fake_arb,
        execution_engine=exec_engine,
        market_titles={ticker: "Task 7 test market"},
    )

    ws_client = KalshiWSClient(
        _FakeKalshi(),
        on_book_update=detector.on_book_update,
        connect_fn=lambda url, additional_headers=None: fake_ws,
    )

    try:
        await asyncio.wait_for(ws_client.run([ticker]), timeout=2)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # The arb engine should have been called twice (snapshot + delta)
    assert fake_arb.call_count == 2, (
        f"Expected 2 arb engine calls (snapshot + delta), got {fake_arb.call_count}"
    )
    # Exactly ONE signal submitted (only the delta produces total_ask < 1.00)
    assert len(exec_engine.submitted) == 1, (
        f"Expected exactly 1 submitted signal, got {len(exec_engine.submitted)}"
    )
    assert exec_engine.submitted[0].market_id == f"kalshi:{ticker}"


async def test_duplicate_suppression_via_shared_engine():
    """Two on_book_update calls through the SAME real ArbEngine instance within 2s
    must yield only ONE submitted signal — the engine's _opportunity_cooldown dedup
    suppresses the second.  This is the cross-path dedup invariant.

    We use a real ArbEngine with zero fees so a bundle-long signal is reliably
    produced, then call on_book_update twice in rapid succession and assert that
    only one signal reaches the execution engine.
    """
    ticker = "TASK7-B"

    # ArbEngine with zero fees: any total_ask < 0.99 produces a signal
    real_engine = ArbEngine(ArbConfig(
        min_edge=0.01,
        bundle_arb_enabled=True,
        mm_enabled=False,
        taker_fee_bps=0,
        maker_fee_bps=0,
        gas_cost_per_order=0.0,
    ))

    exec_engine = _FakeExecEngine()

    detector = WSBundleDetector(
        arb_engine=real_engine,
        execution_engine=exec_engine,
        market_titles={ticker: "Task 7 dedup test market"},
        cooldown_s=0.0,  # Disable detector-level cooldown so only engine cooldown applies
    )

    # Build an OrderBook with total_ask = 0.75 (well below threshold)
    ob = _make_arb_orderbook(f"kalshi:{ticker}", total_ask=0.75)

    # First call — should produce a signal
    await detector.on_book_update(ticker, ob)
    # Second call immediately — engine cooldown (2s) suppresses
    await detector.on_book_update(ticker, ob)

    # Cross-path dedup: only ONE signal submitted despite two calls
    assert len(exec_engine.submitted) == 1, (
        f"Cross-path dedup invariant violated: expected 1 submitted signal, "
        f"got {len(exec_engine.submitted)} — shared engine _opportunity_cooldown must suppress duplicates"
    )
