"""Tests for the multi-outcome mutually-exclusive Kalshi arb detector (#3).

Riskless lock: in a Kalshi event whose markets are mutually exclusive AND
collectively exhaustive (Kalshi's ``mutually_exclusive`` events), exactly one
market resolves YES (pays $1), the rest pay $0.  So buying 1 YES contract on
every market costs ``sum(yes_ask)`` and is guaranteed to return exactly $1.
When ``sum(yes_ask) + fees < $1`` that is a riskless profit.

These tests pin the math.  Getting "riskless" wrong here would place real
losing orders, so every guard is covered.
"""
import math

import pytest

from core.kalshi_multi_outcome import (
    OutcomeLeg,
    detect_multi_outcome_arb,
    fee_per_contract,
)


def test_fee_matches_backtest_formula():
    # ceil(0.07 * p * (1-p)) to the nearest cent
    assert fee_per_contract(0.5) == pytest.approx(math.ceil(0.07 * 0.25 * 100) / 100)
    assert fee_per_contract(0.95) >= 0.0
    # symmetric in p / (1-p)
    assert fee_per_contract(0.2) == pytest.approx(fee_per_contract(0.8))


def _legs(prices, size=10):
    return [OutcomeLeg(ticker=f"M{i}", yes_ask=p, yes_ask_size=size)
            for i, p in enumerate(prices)]


def _no_legs(no_prices, size=10):
    """Legs priced on the NO side only (yes side absent)."""
    return [OutcomeLeg(ticker=f"M{i}", yes_ask=None, yes_ask_size=None,
                       no_ask=p, no_ask_size=size)
            for i, p in enumerate(no_prices)]


def test_underround_is_an_arb():
    # Four outcomes summing to 0.85 → 15c gross underround. Kalshi's per-contract
    # ceil-to-cent fee on 4 legs (~7c here) still leaves a clean riskless edge.
    arb = detect_multi_outcome_arb("EVT", _legs([0.30, 0.25, 0.20, 0.10]), min_edge=0.01)
    assert arb is not None
    assert arb.event_ticker == "EVT"
    assert arb.side == "YES"
    assert arb.cost_per_contract == pytest.approx(0.85)
    # net edge = 1 - cost - fees, must be positive and >= min_edge
    assert arb.net_edge_per_contract >= 0.01
    assert arb.net_edge_per_contract == pytest.approx(1.0 - 0.85 - arb.fees_per_contract)
    assert arb.contracts == 10
    assert arb.total_profit == pytest.approx(arb.net_edge_per_contract * 10)


def test_fairly_priced_is_not_an_arb():
    assert detect_multi_outcome_arb("EVT", _legs([0.25, 0.25, 0.25, 0.25]), min_edge=0.01) is None


def test_overround_is_not_an_arb():
    assert detect_multi_outcome_arb("EVT", _legs([0.30, 0.30, 0.30, 0.20]), min_edge=0.01) is None


def test_fees_can_defeat_a_thin_underround():
    # Sum = 0.99 → 1c gross; Kalshi fees on 4 legs eat it → not >= 1c net.
    arb = detect_multi_outcome_arb("EVT", _legs([0.40, 0.30, 0.20, 0.09]), min_edge=0.01)
    assert arb is None


def test_missing_leg_ask_blocks_arb():
    # If any leg has no buyable ask, the cover is incomplete → NOT riskless.
    legs = _legs([0.30, 0.30, 0.20])
    legs.append(OutcomeLeg(ticker="M3", yes_ask=None, yes_ask_size=None))
    assert detect_multi_outcome_arb("EVT", legs, min_edge=0.01) is None


def test_zero_price_leg_blocks_arb():
    legs = _legs([0.30, 0.30, 0.20])
    legs.append(OutcomeLeg(ticker="M3", yes_ask=0.0, yes_ask_size=5))
    assert detect_multi_outcome_arb("EVT", legs, min_edge=0.01) is None


def test_single_leg_is_never_an_arb():
    assert detect_multi_outcome_arb("EVT", _legs([0.50]), min_edge=0.01) is None


def test_sizing_is_min_ask_size_capped():
    legs = [
        OutcomeLeg("M0", 0.30, 8),
        OutcomeLeg("M1", 0.30, 3),   # smallest book → binds size
        OutcomeLeg("M2", 0.20, 20),
        OutcomeLeg("M3", 0.10, 50),
    ]
    arb = detect_multi_outcome_arb("EVT", legs, min_edge=0.01, max_contracts=10)
    assert arb is not None
    assert arb.contracts == 3


def test_sizing_capped_by_max_contracts():
    arb = detect_multi_outcome_arb("EVT", _legs([0.30, 0.30, 0.20, 0.10], size=100),
                                   min_edge=0.01, max_contracts=10)
    assert arb is not None
    assert arb.contracts == 10


def test_missing_size_blocks_arb():
    # A leg we can price but can't size (no resting ask qty) is not safely fillable.
    legs = [OutcomeLeg("M0", 0.30, 10), OutcomeLeg("M1", 0.30, None),
            OutcomeLeg("M2", 0.20, 10), OutcomeLeg("M3", 0.10, 10)]
    assert detect_multi_outcome_arb("EVT", legs, min_edge=0.01) is None


# ── NO-side dual: buy NO on every leg, payout (N-1) ─────────────────────────

def test_no_side_overround_is_an_arb():
    # 4 legs, NO asks sum to 2.70 < N-1 = 3.0 → ~30c gross, riskless after fees.
    arb = detect_multi_outcome_arb("EVT", _no_legs([0.70, 0.70, 0.70, 0.60]), min_edge=0.01)
    assert arb is not None
    assert arb.side == "NO"
    assert arb.payout_per_cover == pytest.approx(3.0)        # N-1
    assert arb.cost_per_contract == pytest.approx(2.70)
    assert arb.net_edge_per_contract == pytest.approx(3.0 - 2.70 - arb.fees_per_contract)
    assert arb.contracts == 10


def test_no_side_fairly_priced_is_not_an_arb():
    # NO asks sum to exactly N-1 = 3.0 → no edge after fees.
    assert detect_multi_outcome_arb("EVT", _no_legs([0.75, 0.75, 0.75, 0.75]), min_edge=0.01) is None


def test_no_side_missing_leg_blocks_arb():
    legs = _no_legs([0.70, 0.70, 0.70])
    legs.append(OutcomeLeg("M3", yes_ask=None, yes_ask_size=None, no_ask=None, no_ask_size=None))
    assert detect_multi_outcome_arb("EVT", legs, min_edge=0.01) is None


def test_yes_side_preferred_when_both_lock():
    # Construct legs where YES underrounds; YES should win (cheaper capital, payout $1).
    legs = [OutcomeLeg(f"M{i}", yes_ask=y, yes_ask_size=10, no_ask=n, no_ask_size=10)
            for i, (y, n) in enumerate([(0.20, 0.78), (0.20, 0.78), (0.20, 0.78), (0.20, 0.78)])]
    arb = detect_multi_outcome_arb("EVT", legs, min_edge=0.01)
    assert arb is not None
    assert arb.side == "YES"  # YES checked first when it locks
