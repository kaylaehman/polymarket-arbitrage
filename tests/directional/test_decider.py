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
    """AI candidate where Kelly returns positive on YES side (ai_prob=0.70 > market 0.58)."""
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


# ── I2: NO-side Kelly inversion — FAVORABLE candidate sizes positively ─────────

def test_ai_no_side_kelly_inverted_favorable():
    """I2 replacement: favorable NO candidate (ai_prob=0.30 -> P(NO)=0.70, no_price=0.45)
    produces positive Kelly and a sized order >= 1.

    Market YES price = 0.55  → NO price = 0.45
    AI says YES prob = 0.30  → AI P(NO) = 0.70
    NO-space Kelly: yes_price=0.45, ai_prob=0.70 → strongly positive → sizes > 0.
    """
    cand = DirectionalCandidate(
        market_id="kalshi:KX-NO-FAV",
        title="t",
        category="Finance",
        side="NO",
        market_price=0.55,      # YES market price
        ai_probability=0.30,    # AI says YES is only 30% likely → NO is 70% likely
        confidence=0.85,
        edge=0.25,              # |edge| = |0.70 - 0.45| = 0.25
        strategy="ai_directional",
    )
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(cand)
    assert o is not None, "Favorable NO candidate must produce an order"
    assert o.side == "NO"
    assert o.size >= 1


def test_ai_no_side_kelly_unfavorable_returns_none():
    """I2: Unfavorable NO candidate (Kelly non-positive) must return None (no fallback)."""
    # ai_prob=0.70 → P(NO)=0.30, NO price=0.45 → Kelly is negative (unfavorable)
    cand = DirectionalCandidate(
        market_id="kalshi:KX-NO-UNFAV",
        title="t",
        category="Finance",
        side="NO",
        market_price=0.55,   # YES price
        ai_probability=0.70, # AI says YES is 70% likely → NO only 30% → BAD NO bet
        confidence=0.85,
        edge=0.10,           # edge > 0 but Kelly will be negative for NO side
        strategy="ai_directional",
    )
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(cand)
    # Kelly negative → no fallback → must be None
    assert o is None, "Unfavorable NO candidate must return None (no edge-based fallback)"


def test_ai_yes_kelly_zero_no_fallback():
    """I2: When Kelly returns 0 for a YES candidate, return None (no fallback sizing)."""
    # ai_prob=0.50 < yes_price=0.90 → Kelly negative → clipped to 0
    cand = DirectionalCandidate(
        market_id="kalshi:KX-YES-ZERO",
        title="t",
        category="Finance",
        side="YES",
        market_price=0.90,   # expensive YES
        ai_probability=0.50, # AI thinks 50% — not worth it at 90 cents
        confidence=0.85,
        edge=0.05,           # small positive edge but Kelly is non-positive
        strategy="ai_directional",
    )
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(cand)
    assert o is None, "Zero/negative Kelly must return None with no fallback"


def test_ai_yes_side_kelly_sizes_positively():
    """YES-side Kelly with favorable probability sizes > 0."""
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(ai_cand(side="YES"))
    assert o is not None and o.side == "YES"
    assert o.size >= 1
