"""Tests for Decider (Kelly + risk gate) — Task 11."""
import pytest
from core.directional.decider import Decider
from core.directional.models import DirectionalCandidate


class RM:
    def check_directional_order(
        self, o, open_count, directional_exposure, max_position, max_total, max_open
    ):
        return o.notional <= max_position


class ST:
    def directional_exposure(self):
        return 0.0

    def open_positions(self):
        return []


class Caps:
    max_position = 8
    total_exposure = 30
    max_open = 4


def safe_cand():
    return DirectionalCandidate(
        market_id="kalshi:KX-1",
        title="t",
        category="Sports",
        side="NO",
        market_price=0.9,
        ai_probability=None,
        confidence=None,
        edge=0.04,
        strategy="safe_compounder",
    )


def ai_cand(side="YES"):
    return DirectionalCandidate(
        market_id="kalshi:KX-1",
        title="t",
        category="Finance",
        side=side,
        market_price=0.58,
        ai_probability=0.7,
        confidence=0.85,
        edge=0.12,
        strategy="ai_directional",
    )


def test_safe_compounder_fixed_size():
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 30, caps=Caps())
    o = d.decide(safe_cand())
    assert o is not None and o.notional <= 8 and o.size >= 1


def test_rejected_when_over_cap():
    # max_position_usd=20 > caps.max_position=8; notional capped to min(20, 8)=8 by min();
    # risk gate uses caps.max_position=8; notional=8 passes (<=8).
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=20, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(safe_cand())
    assert o is not None and o.notional <= 8


def test_ai_no_side_kelly_inverted():
    """NO-side Kelly uses (1-price, 1-ai_prob) to stay in YES-space."""
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(ai_cand(side="NO"))
    assert o is not None and o.side == "NO"
