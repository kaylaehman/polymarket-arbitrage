import pytest
from music_intel.calibration import (
    brier_score, calibration_curve, apply_calibration, backtest,
)


def test_brier_perfect():
    assert brier_score([1.0, 0.0, 1.0], [1, 0, 1]) == 0.0


def test_brier_worst():
    assert brier_score([0.0, 1.0], [1, 0]) == pytest.approx(1.0)


def test_brier_empty_is_one():
    assert brier_score([], []) == 1.0


def test_calibration_curve_bins_and_freq():
    preds = [0.05, 0.08, 0.92, 0.95]
    outs = [0, 0, 1, 1]
    curve = calibration_curve(preds, outs, bins=10)
    assert curve[0]["observed_freq"] == 0.0   # low bin -> never happened
    assert curve[-1]["observed_freq"] == 1.0  # high bin -> always happened
    assert sum(b["n"] for b in curve) == 4


def test_apply_calibration_maps_through_curve():
    curve = [{"bin_lo": 0.9, "bin_hi": 1.0, "mean_pred": 0.95, "observed_freq": 0.7, "n": 10}]
    assert apply_calibration(0.95, curve) == 0.7    # overconfident 0.95 -> 0.7
    assert apply_calibration(0.3, curve) == 0.3     # no covering bin -> passthrough


def test_apply_calibration_empty_passthrough():
    assert apply_calibration(0.42, []) == 0.42


def test_backtest_out_of_sample_reported():
    # 4 weeks; project_fn predicts the actual #1 confidently in train, mixed in test
    weeks = [
        ("w1", ["recs"], "a"), ("w2", ["recs"], "b"),
        ("w3", ["recs"], "c"), ("w4", ["recs"], "d"),
    ]
    # predicts the right key w/ prob 0.9 every week -> outcome always 1
    def project_fn(records):
        return 0.9, project_fn._keys.pop(0)
    project_fn._keys = ["a", "b", "x", "d"]   # w3 wrong
    res = backtest(weeks, project_fn, train_frac=0.5)
    assert res.n == 4
    assert len(res.predictions) == 4
    assert 0.0 <= res.brier_out_of_sample <= 1.0
    # train weeks (w1,w2) both correct -> in-sample brier low
    assert res.brier_in_sample < 0.05
