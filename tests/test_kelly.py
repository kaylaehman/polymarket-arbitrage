"""Tests for core.kelly — fractional Kelly position sizing (FEAT-05)."""

from core.kelly import kelly_fraction


def test_positive_edge_returns_positive_fraction():
    # AI thinks 0.7 vs market 0.5: real edge, high confidence -> bet something.
    f = kelly_fraction(edge=0.05, yes_price=0.5, ai_probability=0.7, confidence=0.8)
    assert 0.0 < f <= 0.10


def test_capped_at_max_fraction():
    # Huge perceived edge still capped by max_fraction.
    f = kelly_fraction(
        edge=0.5, yes_price=0.5, ai_probability=0.99, confidence=0.9,
        fraction=1.0, max_fraction=0.10,
    )
    assert f == 0.10


def test_no_edge_returns_zero():
    # AI agrees with market (p == price) -> Kelly is zero, clamped to 0.
    f = kelly_fraction(edge=0.0, yes_price=0.5, ai_probability=0.5, confidence=0.9)
    assert f == 0.0


def test_negative_edge_clamped_to_zero():
    # AI thinks YES is overpriced -> never returns a negative size.
    f = kelly_fraction(edge=0.0, yes_price=0.6, ai_probability=0.4, confidence=0.9)
    assert f == 0.0


def test_low_confidence_uses_market_implied_probability():
    # confidence < 0.6 ignores ai_probability and uses yes_price + edge.
    f_low = kelly_fraction(edge=0.02, yes_price=0.5, ai_probability=0.95, confidence=0.3)
    f_market = kelly_fraction(edge=0.02, yes_price=0.5, ai_probability=0.52, confidence=0.3)
    # Same p (0.52) regardless of the wildly different ai_probability.
    assert f_low == f_market


def test_fraction_multiplier_scales_down():
    full = kelly_fraction(edge=0.05, yes_price=0.5, ai_probability=0.7,
                          confidence=0.8, fraction=1.0, max_fraction=1.0)
    quarter = kelly_fraction(edge=0.05, yes_price=0.5, ai_probability=0.7,
                             confidence=0.8, fraction=0.25, max_fraction=1.0)
    assert abs(quarter - full * 0.25) < 1e-9


def test_degenerate_prices_return_zero():
    assert kelly_fraction(edge=0.1, yes_price=0.0, ai_probability=0.7, confidence=0.9) == 0.0
    assert kelly_fraction(edge=0.1, yes_price=1.0, ai_probability=0.7, confidence=0.9) == 0.0
