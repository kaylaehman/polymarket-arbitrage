"""Tests for music_intel.artist_projection — annual top-artist projection model."""
import math
import pytest
from music_intel.artist_projection import release_factor, project_top_artist, ArtistProjection


def test_release_factor_boosts_active_releaser():
    assert release_factor(0, None) == pytest.approx(1.0)
    assert release_factor(3, 30) == pytest.approx(1.0 + 0.08 * 3 + 0.10)   # 3 albums + recent
    assert release_factor(50, 1) == pytest.approx(1.6)                       # capped


def test_release_factor_old_release_no_recency_bonus():
    assert release_factor(2, 300) == pytest.approx(1.0 + 0.08 * 2)          # no recency


def test_leader_gets_highest_prob_and_probs_sum_to_one():
    cs = [
        {"name": "A", "daily_rate": 60, "albums_2026": 3, "days_since_release": 30},
        {"name": "B", "daily_rate": 51, "albums_2026": 0, "days_since_release": None},
        {"name": "C", "daily_rate": 40, "albums_2026": 0, "days_since_release": None},
    ]
    res = project_top_artist(cs, days_remaining=185, days_elapsed=180)
    assert [r.name for r in res][0] == "A"            # highest rate + release boost -> #1
    assert sum(r.prob for r in res) == pytest.approx(1.0, abs=1e-6)
    assert res[0].prob > res[1].prob > res[2].prob


def test_release_activity_can_flip_ranking_vs_pure_rate():
    # Two artists: B has a slightly higher rate, A has 3 albums (release boost).
    cs = [
        {"name": "A", "daily_rate": 51, "albums_2026": 3, "days_since_release": 20},
        {"name": "B", "daily_rate": 53, "albums_2026": 0, "days_since_release": None},
    ]
    res = {r.name: r for r in project_top_artist(cs, days_remaining=185, days_elapsed=180)}
    # A's release factor should lift its projection above B despite lower raw rate
    assert res["A"].projected_units > res["B"].projected_units


def test_ytd_estimate_override_used():
    cs = [
        {"name": "BadBunny", "daily_rate": 51, "albums_2026": 0, "days_since_release": None, "ytd_estimate": 15000},
        {"name": "Drake", "daily_rate": 57, "albums_2026": 3, "days_since_release": 30},
    ]
    res = {r.name: r for r in project_top_artist(cs, days_remaining=185, days_elapsed=180)}
    # BadBunny gets the big YTD head start (15000) instead of rate*days_elapsed
    bb_driver = dict(res["BadBunny"].drivers)
    assert bb_driver["ytd"] == pytest.approx(15000)


def test_close_projections_low_confidence_wide_band():
    close = project_top_artist(
        [{"name": "A", "daily_rate": 50, "albums_2026": 0, "days_since_release": None},
         {"name": "B", "daily_rate": 49.5, "albums_2026": 0, "days_since_release": None}],
        days_remaining=185, days_elapsed=180)
    sep = project_top_artist(
        [{"name": "A", "daily_rate": 80, "albums_2026": 0, "days_since_release": None},
         {"name": "B", "daily_rate": 10, "albums_2026": 0, "days_since_release": None}],
        days_remaining=185, days_elapsed=180)
    assert close[0].confidence < sep[0].confidence
    close_band = close[0].prob_high - close[0].prob_low
    sep_band = sep[0].prob_high - sep[0].prob_low
    assert close_band > sep_band            # less confident -> wider band


def test_drivers_present_and_explainable():
    res = project_top_artist([{"name": "A", "daily_rate": 50, "albums_2026": 1, "days_since_release": 10}],
                             days_remaining=185, days_elapsed=180)
    d = dict(res[0].drivers)
    for key in ("ytd", "daily_rate", "release_factor", "projected_units", "rank"):
        assert key in d
