"""
Tests for Task 4 (WSBundleDetector) and Task 5 (decide_detection_mode).
"""

import pytest
from polymarket_client.models import OrderBook, TokenOrderBook, TokenType


def _ob(market_id: str = "kalshi:KX-1") -> OrderBook:
    """Build a trivial unified OrderBook (no levels — just enough to instantiate)."""
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(TokenType.YES),
        no=TokenOrderBook(TokenType.NO),
    )


# ── Task 4: WSBundleDetector ──────────────────────────────────────────────────

from core.kalshi_ws_detector import WSBundleDetector


class FakeArb:
    """Fake arb engine that always returns a fixed list of signals."""

    def __init__(self, sigs: list):
        self._sigs = sigs

    def analyze(self, ms):
        return self._sigs


class FakeExec:
    """Fake execution engine that records every submitted signal."""

    def __init__(self):
        self.submitted: list = []

    async def submit_signal(self, s) -> None:
        self.submitted.append(s)


async def test_detector_submits_then_cooldown():
    """First call submits; second call within cooldown is skipped; third after
    cooldown elapsed submits again."""
    t = [100.0]
    ex = FakeExec()
    det = WSBundleDetector(
        FakeArb(["SIG"]),
        ex,
        market_titles={"KX-1": "question text"},
        cooldown_s=5.0,
        now_fn=lambda: t[0],
    )

    await det.on_book_update("KX-1", _ob())   # first call: submits
    await det.on_book_update("KX-1", _ob())   # within cooldown: skipped

    assert ex.submitted == ["SIG"], "expected exactly one submit inside cooldown window"

    t[0] = 106.0                               # advance past cooldown (100 + 5 < 106)
    await det.on_book_update("KX-1", _ob())   # cooldown elapsed: submits again

    assert ex.submitted == ["SIG", "SIG"]


async def test_detector_no_signal_no_submit():
    """When arb engine returns no signals, execution engine is never called."""
    ex = FakeExec()
    det = WSBundleDetector(FakeArb([]), ex, market_titles={"KX-1": "question text"})
    await det.on_book_update("KX-1", _ob())
    assert ex.submitted == []


async def test_detector_builds_market_state_with_correct_id():
    """WSBundleDetector builds MarketState with market_id = 'kalshi:<ticker>'."""
    captured = []

    class RecordingArb:
        def analyze(self, ms):
            captured.append(ms)
            return []

    ex = FakeExec()
    det = WSBundleDetector(RecordingArb(), ex, market_titles={"KX-5": "my question"})
    await det.on_book_update("KX-5", _ob("kalshi:KX-5"))

    assert captured, "analyze was not called"
    ms = captured[0]
    assert ms.market.market_id == "kalshi:KX-5"
    assert ms.market.question == "my question"


async def test_detector_cooldown_per_ticker():
    """Cooldowns are tracked per ticker — updates on a different ticker still submit."""
    t = [100.0]
    ex = FakeExec()
    det = WSBundleDetector(
        FakeArb(["SIG"]),
        ex,
        market_titles={"KX-A": "a", "KX-B": "b"},
        cooldown_s=5.0,
        now_fn=lambda: t[0],
    )

    await det.on_book_update("KX-A", _ob("kalshi:KX-A"))   # submits for KX-A
    await det.on_book_update("KX-B", _ob("kalshi:KX-B"))   # separate ticker, submits too

    assert len(ex.submitted) == 2


# ── Task 5: decide_detection_mode ─────────────────────────────────────────────

from core.kalshi_ws_detector import decide_detection_mode


def test_mode_ws_when_healthy():
    """Returns ('ws', 'ws') when enabled + connected + fresh message."""
    mode, reason = decide_detection_mode(
        ws_enabled=True,
        ws_state="connected",
        last_message_ts=100.0,
        now=105.0,
        staleness_s=10.0,
    )
    assert mode == "ws"
    assert reason == "ws"


def test_mode_rest_disabled():
    """Returns rest:disabled when ws_enabled is False."""
    mode, reason = decide_detection_mode(
        ws_enabled=False,
        ws_state="connected",
        last_message_ts=100.0,
        now=105.0,
        staleness_s=10.0,
    )
    assert mode == "rest"
    assert reason == "rest:disabled"


def test_mode_rest_disconnected_state():
    """Returns rest:disconnected when state != 'connected'."""
    mode, reason = decide_detection_mode(
        ws_enabled=True,
        ws_state="reconnecting",
        last_message_ts=100.0,
        now=105.0,
        staleness_s=10.0,
    )
    assert mode == "rest"
    assert reason == "rest:disconnected"


def test_mode_rest_disconnected_no_ts():
    """Returns rest:disconnected when last_message_ts is None."""
    mode, reason = decide_detection_mode(
        ws_enabled=True,
        ws_state="connected",
        last_message_ts=None,
        now=105.0,
        staleness_s=10.0,
    )
    assert mode == "rest"
    assert reason == "rest:disconnected"


def test_mode_rest_stale():
    """Returns rest:stale when age >= staleness_s."""
    mode, reason = decide_detection_mode(
        ws_enabled=True,
        ws_state="connected",
        last_message_ts=90.0,
        now=105.0,
        staleness_s=10.0,   # age = 15 >= 10
    )
    assert mode == "rest"
    assert reason == "rest:stale"
