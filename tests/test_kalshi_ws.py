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
