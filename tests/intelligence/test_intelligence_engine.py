"""Tests for IntelligenceEngine._summarize — the tiered filter/boost policy.

Only the decision policy is exercised here; the news/Claude pipeline is covered
by test_news_fetcher.py and test_ai_analyzer.py. We pass dummy fetcher/analyzer
since _summarize never calls them.
"""

from types import SimpleNamespace

from intelligence.cache import SignalCache
from intelligence.intelligence_engine import IntelligenceEngine
from intelligence.signal import MarketSignal, classify_direction


def _config(mode="both", min_confidence=0.65, min_edge_boost=0.03, min_edge_filter=0.10):
    return SimpleNamespace(
        mode=mode,
        min_confidence=min_confidence,
        min_edge_boost=min_edge_boost,
        min_edge_filter=min_edge_filter,
        news=SimpleNamespace(cache_ttl_minutes=10),
    )


def _engine(config=None):
    return IntelligenceEngine(
        fetcher=None,
        analyzer=None,
        config=config or _config(),
        cache=SignalCache(ttl_minutes=10),
    )


def _signal(ai_prob, price=0.5, confidence=0.8):
    return MarketSignal(
        market_id="m1",
        market_question="Will X?",
        current_yes_price=price,
        ai_probability=ai_prob,
        confidence=confidence,
        direction=classify_direction(ai_prob, price, confidence),
        reasoning="r",
        news_headlines=[],
    )


def test_low_confidence_is_neutral():
    eng = _engine()
    summary = eng._summarize(_signal(ai_prob=0.9, price=0.5, confidence=0.4), arb_edge=0.03)
    assert summary.should_filter is False
    assert summary.should_boost is False
    assert summary.adjusted_edge == 0.03


def test_small_gap_no_action():
    # gap = 0.02, below min_edge_boost (0.03) -> agree, no action.
    eng = _engine()
    summary = eng._summarize(_signal(ai_prob=0.52, price=0.50), arb_edge=0.03)
    assert summary.should_filter is False
    assert summary.should_boost is False


def test_moderate_gap_boosts_not_filters():
    # gap = 0.07: above boost (0.03), below filter (0.10) -> boost only.
    eng = _engine(_config(mode="both"))
    summary = eng._summarize(_signal(ai_prob=0.57, price=0.50), arb_edge=0.03)
    assert summary.should_boost is True
    assert summary.should_filter is False


def test_large_gap_filters():
    # gap = 0.30: at/above filter threshold -> filter (and boost in "both").
    eng = _engine(_config(mode="both"))
    summary = eng._summarize(_signal(ai_prob=0.80, price=0.50), arb_edge=0.03)
    assert summary.should_filter is True
    assert summary.should_boost is True


def test_filter_mode_never_boosts():
    eng = _engine(_config(mode="filter"))
    summary = eng._summarize(_signal(ai_prob=0.80, price=0.50), arb_edge=0.03)
    assert summary.should_filter is True
    assert summary.should_boost is False


def test_boost_mode_never_filters():
    eng = _engine(_config(mode="boost"))
    summary = eng._summarize(_signal(ai_prob=0.80, price=0.50), arb_edge=0.03)
    assert summary.should_filter is False
    assert summary.should_boost is True


def test_adjusted_edge_is_capped():
    # gap = +0.40 but the edge nudge is capped at +/-0.05.
    eng = _engine()
    summary = eng._summarize(_signal(ai_prob=0.90, price=0.50), arb_edge=0.03)
    assert abs(summary.adjusted_edge - 0.08) < 1e-9  # 0.03 + min(0.05, 0.40)
