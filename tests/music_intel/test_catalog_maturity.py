"""Task A: catalog-maturity weighting of the forward projection.

The annual global #1 is structurally a deep-catalog superstar, never a one-album
newcomer. A spike-concentrated artist (small all-time catalog, high current daily
from one hot album) will decay, so their FORWARD streams should be discounted.
catalog_maturity_weight uses total/daily (≈ catalog age in days) as the signal.
"""
import pytest
from music_intel.artist_projection import catalog_maturity_weight, project_top_artist


def test_maturity_weight_off_when_lambda_zero():
    # lam=0 -> no effect regardless of catalog
    assert catalog_maturity_weight(4000.0, 90.0, lam=0.0) == pytest.approx(1.0)


def test_maturity_weight_deep_catalog_near_one():
    # total/daily = 30000/80 = 375 days (> ref 365) -> mature -> ~1.0
    w = catalog_maturity_weight(30000.0, 80.0, lam=1.0)
    assert w == pytest.approx(1.0, abs=1e-6)


def test_maturity_weight_spike_discounted():
    # total/daily = 4000/90 ≈ 44 days -> spike-concentrated -> < 1.0
    w = catalog_maturity_weight(4000.0, 90.0, lam=1.0)
    assert 0.5 <= w < 0.7


def test_maturity_weight_safe_on_bad_inputs():
    assert catalog_maturity_weight(None, 90.0, lam=1.0) == pytest.approx(1.0)
    assert catalog_maturity_weight(4000.0, 0.0, lam=1.0) == pytest.approx(1.0)


def test_maturity_flips_spike_below_deep_catalog():
    # Mirrors 2021: spike artist S has higher raw YTD+daily, deep-catalog M has more
    # total streams. Without maturity S projects #1 (the historical miss); with
    # maturity the deep catalog M wins.
    contenders = [
        {"name": "DeepCatalog", "daily_rate": 80.0, "ytd_estimate": 12000.0, "catalog_total": 30000.0},
        {"name": "ViralSpike", "daily_rate": 90.0, "ytd_estimate": 13000.0, "catalog_total": 4000.0},
    ]
    off = project_top_artist(contenders, days_remaining=125, days_elapsed=240, maturity_lambda=0.0)
    assert off[0].name == "ViralSpike"          # the uncorrected miss
    on = project_top_artist(contenders, days_remaining=125, days_elapsed=240, maturity_lambda=1.0)
    assert on[0].name == "DeepCatalog"          # maturity weighting fixes it
