"""
Tests for core.kelly.kelly_fraction — pins exact behavior so regressions are
caught immediately.

Coverage:
- High vs low confidence branch divergence
- Non-positive EV returns 0.0 (clamped, not raised)
- max_fraction hard cap
- confidence=None does not raise and returns finite >= 0
- Degenerate yes_price (0.0 and 1.0) returns 0.0 without raising
- Output is within [0, max_fraction] for a representative set of sane inputs
"""
import pytest
from core.kelly import kelly_fraction


# ---------------------------------------------------------------------------
# Branch: high-confidence vs low-confidence diverge on the same inputs
# ---------------------------------------------------------------------------

class TestConfidenceBranch:
    """The win probability p comes from ai_probability when confidence >= 0.6,
    and from yes_price + edge when confidence < 0.6.  The two branches must
    produce different fractions when ai_probability differs from yes_price+edge."""

    def test_should_use_ai_probability_when_confidence_high(self):
        # confidence=0.7 -> p = ai_probability=0.55; b=1/0.4-1=1.5
        # raw_kelly = (1.5*0.55 - 0.45)/1.5 = (0.825-0.45)/1.5 = 0.25 => frac=0.25*0.25=0.0625
        result = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                confidence=0.7)
        assert result == pytest.approx(0.0625, abs=1e-9)

    def test_should_use_market_implied_when_confidence_low(self):
        # confidence=0.3 -> p = yes_price+edge = 0.4+0.07 = 0.47; b=1.5
        # raw_kelly = (1.5*0.47 - 0.53)/1.5 = (0.705-0.53)/1.5 = 0.1167 => frac*0.25~0.0292
        result = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                confidence=0.3)
        assert result == pytest.approx(0.029166666666666674, abs=1e-9)

    def test_should_differ_across_confidence_boundary(self):
        """Flipping confidence across 0.6 must change the result when ai_prob != yes_price+edge."""
        high = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55, confidence=0.6)
        low  = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55, confidence=0.59)
        assert high != pytest.approx(low), (
            "High-confidence and low-confidence branches must diverge when "
            "ai_probability != yes_price+edge"
        )

    def test_should_treat_none_confidence_as_low_confidence(self):
        """confidence=None is coerced to 0.0 by `(confidence or 0.0)`, so it
        falls into the low-confidence branch — same result as confidence=0.0."""
        result_none = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                     confidence=None)
        result_zero = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                     confidence=0.0)
        assert result_none == pytest.approx(result_zero, abs=1e-12)


# ---------------------------------------------------------------------------
# Non-positive EV -> 0.0 (no bet)
# ---------------------------------------------------------------------------

class TestNonPositiveEdge:
    """When Kelly's raw_kelly is <= 0, max(0.0, ...) clamps to exactly 0.0."""

    def test_should_return_zero_when_ai_probability_below_yes_price_at_high_confidence(self):
        # High confidence -> p = ai_probability=0.5, yes_price=0.6 -> odds b=0.667
        # raw_kelly = (0.667*0.5 - 0.5)/0.667 = (0.333-0.5)/0.667 = -0.25 -> clamped to 0
        result = kelly_fraction(edge=0.0, yes_price=0.6, ai_probability=0.5, confidence=0.9)
        assert result == 0.0

    def test_should_return_zero_when_market_implied_equals_price_at_low_confidence(self):
        # Low confidence, edge=0 -> p = yes_price+0 = yes_price
        # raw_kelly = (b*p - q)/b = (b*yes_price - (1-yes_price))/b = yes_price - (1-yes_price)/b
        # At yes_price=0.5, b=1.0: raw_kelly = (1*0.5-0.5)/1 = 0, fraction*0 = 0
        result = kelly_fraction(edge=0.0, yes_price=0.5, ai_probability=0.99, confidence=0.0)
        assert result == pytest.approx(0.0, abs=1e-9)

    def test_should_return_zero_when_market_implied_sum_exceeds_one(self):
        # Low confidence, yes_price+edge >= 1.0 -> p >= 1 -> guard returns 0
        result = kelly_fraction(edge=0.5, yes_price=0.6, ai_probability=0.5, confidence=0.3)
        assert result == 0.0

    def test_should_return_zero_when_ai_probability_is_one_at_high_confidence(self):
        # p=1.0 triggers the p>=1 guard
        result = kelly_fraction(edge=0.05, yes_price=0.5, ai_probability=1.0, confidence=0.9)
        assert result == 0.0

    def test_should_return_zero_when_ai_probability_is_zero_at_high_confidence(self):
        # p=0 triggers the p<=0 guard
        result = kelly_fraction(edge=0.05, yes_price=0.5, ai_probability=0.0, confidence=0.9)
        assert result == 0.0


# ---------------------------------------------------------------------------
# max_fraction hard cap
# ---------------------------------------------------------------------------

class TestMaxFractionCap:
    """A dominant edge must be clipped to max_fraction, not allowed to exceed it."""

    def test_should_cap_output_at_default_max_fraction(self):
        # Very strong edge: yes_price=0.1, ai_prob=0.9, confidence=0.9
        # Kelly will produce a huge fraction; capped at default max_fraction=0.10
        result = kelly_fraction(edge=0.4, yes_price=0.1, ai_probability=0.9,
                                confidence=0.9)
        assert result == pytest.approx(0.10, abs=1e-9)

    def test_should_respect_custom_max_fraction(self):
        result = kelly_fraction(edge=0.4, yes_price=0.1, ai_probability=0.9,
                                confidence=0.9, max_fraction=0.05)
        assert result == pytest.approx(0.05, abs=1e-9)

    def test_should_not_exceed_max_fraction_for_moderate_edge(self):
        result = kelly_fraction(edge=0.07, yes_price=0.3, ai_probability=0.5,
                                confidence=0.7, max_fraction=0.10)
        assert result <= 0.10


# ---------------------------------------------------------------------------
# confidence=None guard
# ---------------------------------------------------------------------------

class TestConfidenceNoneGuard:
    """confidence=None must not raise and must return a finite, non-negative value."""

    def test_should_not_raise_when_confidence_is_none(self):
        result = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                confidence=None)
        assert isinstance(result, float)

    def test_should_return_non_negative_when_confidence_is_none(self):
        result = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                confidence=None)
        assert result >= 0.0

    def test_should_return_finite_value_when_confidence_is_none(self):
        import math
        result = kelly_fraction(edge=0.07, yes_price=0.4, ai_probability=0.55,
                                confidence=None)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# Degenerate yes_price at boundaries
# ---------------------------------------------------------------------------

class TestDegeneratePrice:
    """yes_price=0.0 and yes_price=1.0 are guarded (b <= 0 or yes_price >= 1)
    and return 0.0 without raising."""

    def test_should_return_zero_and_not_raise_when_yes_price_is_zero(self):
        result = kelly_fraction(edge=0.07, yes_price=0.0, ai_probability=0.55,
                                confidence=0.7)
        assert result == 0.0

    def test_should_return_zero_and_not_raise_when_yes_price_is_one(self):
        result = kelly_fraction(edge=0.07, yes_price=1.0, ai_probability=0.55,
                                confidence=0.7)
        assert result == 0.0

    def test_should_return_zero_and_not_raise_when_yes_price_is_very_close_to_one(self):
        # 0.9999 passes the guard but b is tiny; result should still be in bounds
        result = kelly_fraction(edge=0.0, yes_price=0.9999, ai_probability=0.9999,
                                confidence=0.7)
        assert 0.0 <= result <= 0.10

    def test_should_return_non_negative_when_yes_price_is_very_small(self):
        # 0.001 passes the guard; result in [0, max_fraction]
        result = kelly_fraction(edge=0.1, yes_price=0.001, ai_probability=0.9,
                                confidence=0.7)
        assert 0.0 <= result <= 0.10


# ---------------------------------------------------------------------------
# Property: output always in [0, max_fraction] for sane inputs
# ---------------------------------------------------------------------------

class TestOutputBounds:
    """A range of valid inputs must all produce values within [0.0, 0.10]."""

    _SANE_INPUTS = [
        (0.07, 0.3, 0.5,  0.7),
        (0.05, 0.5, 0.6,  0.4),
        (0.02, 0.8, 0.85, 0.9),
        (0.15, 0.2, 0.7,  0.8),
        (0.0,  0.5, 0.5,  0.5),
        (-0.01, 0.4, 0.45, 0.6),
        (0.1,  0.6, 0.72, 0.0),
        (0.07, 0.4, 0.55, None),  # None confidence
    ]

    @pytest.mark.parametrize("edge,yes_price,ai_prob,conf", _SANE_INPUTS)
    def test_should_be_within_max_fraction_bounds(self, edge, yes_price, ai_prob, conf):
        result = kelly_fraction(edge, yes_price, ai_prob, conf)
        assert 0.0 <= result <= 0.10, (
            f"Out of bounds: {result} for edge={edge}, yes_price={yes_price}, "
            f"ai_prob={ai_prob}, conf={conf}"
        )
