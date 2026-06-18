"""Tests for intelligence.signal — dataclasses and classify_direction."""

from datetime import datetime

from intelligence.signal import MarketSignal, SignalSummary, classify_direction


def _signal(ai_prob=0.7, price=0.6, confidence=0.8) -> MarketSignal:
    return MarketSignal(
        market_id="m1",
        market_question="Will X happen?",
        current_yes_price=price,
        ai_probability=ai_prob,
        confidence=confidence,
        direction=classify_direction(ai_prob, price, confidence),
        reasoning="because",
        news_headlines=["headline"],
        timestamp=datetime.utcnow(),
    )


def test_market_signal_edge_vs_market():
    sig = _signal(ai_prob=0.7, price=0.6)
    assert abs(sig.edge_vs_market - 0.1) < 1e-9


def test_signal_summary_construction():
    sig = _signal()
    summary = SignalSummary(
        signal=sig,
        should_filter=True,
        should_boost=False,
        adjusted_edge=0.04,
        reason="test",
    )
    assert summary.signal is sig
    assert summary.should_filter is True
    assert summary.adjusted_edge == 0.04


def test_signal_summary_neutral():
    summary = SignalSummary.neutral(arb_edge=0.03)
    assert summary.signal is None
    assert summary.should_filter is False
    assert summary.should_boost is False
    assert summary.adjusted_edge == 0.03


def test_classify_direction_uncertain_below_confidence():
    # Big delta but confidence below 0.5 -> uncertain wins.
    assert classify_direction(ai_prob=0.9, market_price=0.5, confidence=0.49) == "uncertain"


def test_classify_direction_confidence_exactly_half_is_not_uncertain():
    # confidence == 0.5 is NOT below 0.5, so it classifies normally.
    assert classify_direction(ai_prob=0.9, market_price=0.5, confidence=0.5) == "bullish"


def test_classify_direction_bullish_and_bearish():
    assert classify_direction(ai_prob=0.7, market_price=0.6, confidence=0.8) == "bullish"
    assert classify_direction(ai_prob=0.5, market_price=0.6, confidence=0.8) == "bearish"


def test_classify_direction_boundary_is_exclusive():
    # The 0.05 boundary is exclusive: just inside -> "agree", just outside ->
    # directional. (Exact-0.05 equality is avoided here because floating-point
    # makes 0.65 - 0.60 evaluate to slightly above 0.05.)
    assert classify_direction(ai_prob=0.649, market_price=0.60, confidence=0.8) == "agree"
    assert classify_direction(ai_prob=0.551, market_price=0.60, confidence=0.8) == "agree"
    assert classify_direction(ai_prob=0.66, market_price=0.60, confidence=0.8) == "bullish"
    assert classify_direction(ai_prob=0.54, market_price=0.60, confidence=0.8) == "bearish"
