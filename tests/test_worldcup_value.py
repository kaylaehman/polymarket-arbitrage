"""
tests/test_worldcup_value.py — Unit tests for core.worldcup package.

10 tests covering config, recalibrate, simulate, value_detector, and ledger.
Uses pytest + tmp_path; no network calls; no live execution.
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# 1. Config constants are sane
# ---------------------------------------------------------------------------

def test_config_constants():
    from core.worldcup.config import (
        VALUE_MARGIN,
        KELLY_FRACTION,
        PAPER_BANKROLL,
        N_SIMULATIONS,
        MIN_LIQUIDITY,
    )
    assert 0 < VALUE_MARGIN < 1
    assert 0 < KELLY_FRACTION <= 1
    assert PAPER_BANKROLL > 0
    assert N_SIMULATIONS >= 1000
    assert MIN_LIQUIDITY >= 0


# ---------------------------------------------------------------------------
# 2. _gmult is correct for boundary values
# ---------------------------------------------------------------------------

def test_gmult_boundaries():
    from core.worldcup.recalibrate import _gmult
    assert _gmult(0) == 1.0
    assert _gmult(1) == 1.0
    assert _gmult(2) == 1.5
    assert _gmult(3) == pytest.approx((11 + 3) / 8.0)
    assert _gmult(5) == pytest.approx((11 + 5) / 8.0)


# ---------------------------------------------------------------------------
# 3. _expected_score is a valid probability
# ---------------------------------------------------------------------------

def test_expected_score_range():
    from core.worldcup.recalibrate import _expected_score
    for ra, rb, bonus in [(1800, 1600, 0), (1500, 1500, 0), (1200, 2000, 37.5)]:
        es = _expected_score(ra, rb, bonus)
        assert 0 < es < 1, f"expected_score out of range for ra={ra} rb={rb} bonus={bonus}"


# ---------------------------------------------------------------------------
# 4. apply_results moves ratings in the right direction (winner goes up)
# ---------------------------------------------------------------------------

def test_apply_results_winner_gains_elo():
    from core.worldcup.recalibrate import apply_results
    base = {"argentina": 1976.0, "canada": 1750.0}
    updated = apply_results(base, [("argentina", "canada", 2, 0)])
    # Argentina wins — should gain Elo; Canada should lose
    assert updated["argentina"] > base["argentina"]
    assert updated["canada"] < base["canada"]
    # Elo is zero-sum in this simple two-team case
    assert updated["argentina"] + updated["canada"] == pytest.approx(
        base["argentina"] + base["canada"]
    )


# ---------------------------------------------------------------------------
# 5. apply_results with a draw moves ratings toward equalisation
# ---------------------------------------------------------------------------

def test_apply_results_draw_equalises():
    from core.worldcup.recalibrate import apply_results
    # Stronger team (higher rating) draws: should lose a little Elo
    base = {"argentina": 2000.0, "canada": 1600.0}
    updated = apply_results(base, [("argentina", "canada", 1, 1)])
    assert updated["argentina"] < base["argentina"]
    assert updated["canada"] > base["canada"]


# ---------------------------------------------------------------------------
# 6. load_and_recalibrate returns a dict with all keys from base ratings
# ---------------------------------------------------------------------------

def test_load_and_recalibrate_returns_all_teams():
    from core.worldcup.recalibrate import load_and_recalibrate
    ratings = load_and_recalibrate()
    # Should include all WC2026 participant slugs used in WC2026_RESULTS
    for slug in ("argentina", "france", "spain", "brazil", "england"):
        assert slug in ratings, f"Missing team slug: {slug}"
    # All values are floats
    assert all(isinstance(v, float) for v in ratings.values())


# ---------------------------------------------------------------------------
# 7. simulate_tournament returns probabilities that sum to 1
# ---------------------------------------------------------------------------

def test_simulate_tournament_probs_sum_to_one():
    from core.worldcup.recalibrate import load_and_recalibrate
    from core.worldcup.simulate import simulate_tournament
    ratings = load_and_recalibrate()
    probs = simulate_tournament(ratings, n_simulations=500, seed=42)
    total = sum(probs.values())
    assert total == pytest.approx(1.0, abs=0.02), f"Probabilities sum to {total}, expected ~1.0"


# ---------------------------------------------------------------------------
# 8. simulate_tournament: known strong team has higher win prob than weak team
# ---------------------------------------------------------------------------

def test_simulate_tournament_strong_team_higher_prob():
    from core.worldcup.recalibrate import load_and_recalibrate
    from core.worldcup.simulate import simulate_tournament, ADVANCED_32
    ratings = load_and_recalibrate()
    probs = simulate_tournament(ratings, n_simulations=1000, seed=99)
    # Argentina (highest Elo) should beat New Zealand win probability
    if "argentina" in probs and "new-zealand" in probs:
        assert probs["argentina"] > probs["new-zealand"]


# ---------------------------------------------------------------------------
# 9. Ledger: record, retrieve, and resolve a paper bet
# ---------------------------------------------------------------------------

def test_ledger_record_and_resolve(tmp_path):
    from core.worldcup.ledger import Ledger
    db = tmp_path / "test_bets.db"
    ledger = Ledger(db_path=db)

    bet_id = ledger.record_bet(
        slug="tec-f-wc-2026-07-19-winner-arg",
        outcome_type="tournament_winner",
        team_slug="argentina",
        model_prob=0.22,
        market_price=0.14,
        edge=0.08,
        stake=25.0,
    )
    assert isinstance(bet_id, int) and bet_id > 0

    open_bets = ledger.get_open_bets()
    assert len(open_bets) == 1
    assert open_bets[0].team_slug == "argentina"
    assert open_bets[0].status == "open"

    ledger.resolve_bet(bet_id, won=True)
    open_bets_after = ledger.get_open_bets()
    assert len(open_bets_after) == 0

    all_bets = ledger.get_all_bets()
    assert all_bets[0].status == "won"
    assert all_bets[0].pnl > 0


# ---------------------------------------------------------------------------
# 10. value_detector: returns ValueBet when edge exceeds margin
# ---------------------------------------------------------------------------

def test_value_detector_finds_edge():
    from core.worldcup.value_detector import detect_value, ValueBet

    # Fake sim_probs: argentina has 0.25 win probability
    sim_probs = {"argentina": 0.25, "france": 0.18, "spain": 0.15}

    # Fake market: argentina winner priced at 0.15 (edge = 0.10 > VALUE_MARGIN=0.07)
    markets = [
        {
            "slug": "tec-f-wc-2026-07-19-winner-arg",
            "liquidity": 1000,
            "marketSides": [
                {"outcome": "YES", "ask": "0.15"},
                {"outcome": "NO",  "ask": "0.87"},
            ],
        }
    ]

    bets = detect_value(sim_probs, markets, value_margin=0.07)
    assert len(bets) == 1
    vb = bets[0]
    assert vb.team_slug == "argentina"
    assert vb.edge == pytest.approx(0.25 - 0.15, abs=0.001)
    assert vb.kelly_stake > 0


def test_value_detector_no_edge_below_margin():
    from core.worldcup.value_detector import detect_value

    sim_probs = {"spain": 0.18}
    markets = [
        {
            "slug": "tec-f-wc-2026-07-19-winner-esp",
            "liquidity": 1000,
            "marketSides": [
                {"outcome": "YES", "ask": "0.16"},  # edge = 0.02 < 0.07
            ],
        }
    ]
    bets = detect_value(sim_probs, markets, value_margin=0.07)
    assert len(bets) == 0
