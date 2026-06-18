"""Tests for CrossPlatformMonitor — pairing OBs, AI annotation, publishing."""

import pytest

from core.cross_platform_arb import CrossPlatformOpportunity, MarketPair
from core.cross_platform_monitor import CrossPlatformMonitor
from intelligence.signal import MarketSignal, SignalSummary
from polymarket_client.models import (
    OrderBook, OrderBookSide, PriceLevel, TokenOrderBook, TokenType,
)


def _ob(yb=0.40, ya=0.42, nb=0.40, na=0.42):
    return OrderBook(
        market_id="m",
        yes=TokenOrderBook(token_type=TokenType.YES,
                           bids=OrderBookSide(levels=[PriceLevel(yb, 100)]),
                           asks=OrderBookSide(levels=[PriceLevel(ya, 100)])),
        no=TokenOrderBook(token_type=TokenType.NO,
                          bids=OrderBookSide(levels=[PriceLevel(nb, 100)]),
                          asks=OrderBookSide(levels=[PriceLevel(na, 100)])),
    )


def _pair():
    return MarketPair(polymarket_id="poly1", kalshi_ticker="KX1",
                      polymarket_question="Will the Fed hike in June 2026?",
                      kalshi_title="Fed hike June 2026", similarity_score=0.9,
                      category="finance")


class _FakeFeed:
    def __init__(self, ob): self._ob = ob
    def get_order_book(self, mid): return self._ob


class _FakeKalshi:
    def __init__(self, ob): self._ob = ob
    async def get_orderbook_unified(self, ticker): return self._ob


class _FakeEngine:
    def __init__(self, opp): self._opp = opp
    def check_arbitrage(self, pair, poly_ob, kalshi_ob): return self._opp


class _FakeIntel:
    def __init__(self, summary): self._s = summary; self.calls = []
    async def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        return self._s


class _FakeDash:
    def __init__(self): self.published = []
    def add_cross_platform_opportunity(self, d): self.published.append(d)


def _opp():
    return CrossPlatformOpportunity(
        opportunity_id="x", market_pair=_pair(), buy_platform="polymarket",
        sell_platform="kalshi", token="YES", buy_price=0.42, sell_price=0.50,
        gross_edge=0.08, net_edge=0.06, edge_pct=0.14,
    )


def _summary(direction="bullish", confidence=0.8):
    sig = MarketSignal("poly1|KX1", "q", 0.41, 0.7, confidence, direction, "news", [])
    return SignalSummary(signal=sig, should_filter=False, should_boost=True,
                         adjusted_edge=0.07, reason=f"AI {direction}")


async def test_no_poly_orderbook_returns_none():
    mon = CrossPlatformMonitor(_FakeEngine(_opp()), _FakeFeed(None),
                               _FakeKalshi(_ob()), lambda: [_pair()])
    assert await mon.evaluate_pair(_pair()) is None


async def test_no_arb_returns_none_and_no_publish():
    dash = _FakeDash()
    mon = CrossPlatformMonitor(_FakeEngine(None), _FakeFeed(_ob()),
                               _FakeKalshi(_ob()), lambda: [_pair()], dashboard=dash)
    assert await mon.evaluate_pair(_pair()) is None
    assert dash.published == []


async def test_arb_publishes_without_intelligence():
    dash = _FakeDash()
    mon = CrossPlatformMonitor(_FakeEngine(_opp()), _FakeFeed(_ob()),
                               _FakeKalshi(_ob()), lambda: [_pair()], dashboard=dash)
    opp = await mon.evaluate_pair(_pair())
    assert opp is not None
    assert opp.signal is None
    assert len(dash.published) == 1
    assert dash.published[0]["ai_direction"] is None  # no intelligence


async def test_arb_annotated_and_published_with_intelligence():
    dash = _FakeDash()
    intel = _FakeIntel(_summary("bullish", 0.8))
    mon = CrossPlatformMonitor(
        _FakeEngine(_opp()), _FakeFeed(_ob()), _FakeKalshi(_ob()),
        lambda: [_pair()], intelligence_engine=intel, intel_enabled=True, dashboard=dash,
    )
    opp = await mon.evaluate_pair(_pair())
    assert opp.signal is not None and opp.signal.signal.direction == "bullish"
    # evaluate() received the cross-platform pair id + the arb edge
    assert intel.calls[0]["market_id"] == _pair().pair_id  # "poly:poly1|kalshi:KX1"
    assert intel.calls[0]["arb_edge"] == 0.06
    pub = dash.published[0]
    assert pub["ai_direction"] == "bullish"
    assert pub["ai_confidence"] == 0.8


async def test_intel_failure_does_not_break_evaluation():
    class _BoomIntel:
        async def evaluate(self, **kw): raise RuntimeError("claude down")
    dash = _FakeDash()
    mon = CrossPlatformMonitor(
        _FakeEngine(_opp()), _FakeFeed(_ob()), _FakeKalshi(_ob()),
        lambda: [_pair()], intelligence_engine=_BoomIntel(), intel_enabled=True, dashboard=dash,
    )
    opp = await mon.evaluate_pair(_pair())   # must still return the opp
    assert opp is not None
    assert len(dash.published) == 1          # still published despite AI failure


async def test_poll_once_counts_opportunities():
    mon = CrossPlatformMonitor(_FakeEngine(_opp()), _FakeFeed(_ob()),
                               _FakeKalshi(_ob()), lambda: [_pair(), _pair()])
    assert await mon.poll_once() == 2
