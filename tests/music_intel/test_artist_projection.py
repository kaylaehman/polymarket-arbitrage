"""Tests for music_intel.artist_projection — annual top-artist projection model."""
import math
import pytest
from music_intel.artist_projection import release_volatility, project_top_artist, ArtistProjection


def test_release_volatility_recent_active_is_higher():
    from music_intel.artist_projection import release_volatility
    assert release_volatility(0, None) == pytest.approx(1.0)               # no releases -> baseline
    assert release_volatility(3, 30) == pytest.approx(1.0 + 0.5 + 0.1*3)   # recent + 3 albums
    assert release_volatility(50, 1) == pytest.approx(2.0)                  # capped


def test_release_does_NOT_change_projection_point_estimate():
    # two artists, identical ytd + rate, different release activity -> SAME projected_units
    base = {"daily_rate":50,"ytd_estimate":10000}
    res = {r.name:r for r in project_top_artist(
        [{"name":"Active","albums_2026":3,"days_since_release":20, **base},
         {"name":"Quiet","albums_2026":0,"days_since_release":None, **base}],
        days_remaining=185, days_elapsed=180)}
    assert res["Active"].projected_units == pytest.approx(res["Quiet"].projected_units)


def test_recent_releaser_gets_wider_band():
    base = {"daily_rate":50,"ytd_estimate":10000}
    res = {r.name:r for r in project_top_artist(
        [{"name":"Active","albums_2026":3,"days_since_release":20, **base},
         {"name":"Quiet","albums_2026":0,"days_since_release":None, **base}],
        days_remaining=185, days_elapsed=180)}
    # identical prob (same projection) but Active's band is wider (more volatile)
    assert res["Active"].prob == pytest.approx(res["Quiet"].prob)
    assert (res["Active"].prob_high-res["Active"].prob_low) > (res["Quiet"].prob_high-res["Quiet"].prob_low)


def test_higher_projection_wins_no_release_boost():
    # Bad-Bunny-like: higher YTD, lower rate, no recent album -> still beats Drake-like on projection
    res = project_top_artist(
        [{"name":"BadBunny","daily_rate":51.2,"ytd_estimate":12852,"albums_2026":0,"days_since_release":140},
         {"name":"Drake","daily_rate":57.7,"ytd_estimate":10978,"albums_2026":3,"days_since_release":45}],
        days_remaining=185, days_elapsed=180)
    by = {r.name:r for r in res}
    # BadBunny 12852+51.2*185=22324 ; Drake 10978+57.7*185=21652.5 -> BadBunny leads (no phantom boost)
    assert by["BadBunny"].projected_units > by["Drake"].projected_units
    assert res[0].name == "BadBunny"


def test_leader_gets_highest_prob_and_probs_sum_to_one():
    cs = [
        {"name": "A", "daily_rate": 60, "albums_2026": 3, "days_since_release": 30},
        {"name": "B", "daily_rate": 51, "albums_2026": 0, "days_since_release": None},
        {"name": "C", "daily_rate": 40, "albums_2026": 0, "days_since_release": None},
    ]
    res = project_top_artist(cs, days_remaining=185, days_elapsed=180)
    assert [r.name for r in res][0] == "A"            # highest rate -> #1
    assert sum(r.prob for r in res) == pytest.approx(1.0, abs=1e-6)
    assert res[0].prob > res[1].prob > res[2].prob


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
    for key in ("ytd", "daily_rate", "release_volatility", "projected_units", "rank"):
        assert key in d
