"""Reliability (calibration) report: given (predicted p_yes, actual outcome) pairs,
bin by predicted probability and compare predicted vs actual frequency, so we can
see whether the climate model is over/under-confident and tighten sigma."""
import pytest
from core.directional.climate.reliability import reliability_bins, brier_score, calibration_verdict


def test_reliability_bins_group_and_frequency():
    # 10 predictions at ~0.05 (low bin), 4 of which actually happened -> actual 0.4
    rows = [(0.05, 1)] * 4 + [(0.05, 0)] * 6
    bins = reliability_bins(rows, n_bins=10)
    assert len(bins) == 1
    b = bins[0]
    assert b["n"] == 10
    assert b["pred_mean"] == pytest.approx(0.05)
    assert b["actual_freq"] == pytest.approx(0.4)   # model said 5%, reality 40% -> UNDERconfident


def test_brier_score():
    # perfect predictions -> brier 0
    assert brier_score([(1.0, 1), (0.0, 0)]) == pytest.approx(0.0)
    # always-0.5 on a 50/50 -> 0.25
    assert brier_score([(0.5, 1), (0.5, 0)]) == pytest.approx(0.25)
    assert brier_score([]) is None


def test_calibration_verdict_overconfident():
    # model predicts high (0.7 mean) but reality is low (0.2) -> OVERCONFIDENT (sigma too wide/high bias)
    rows = [(0.7, 0)] * 16 + [(0.7, 1)] * 4
    v = calibration_verdict(rows)
    assert v["n"] == 20
    assert v["pred_mean"] == pytest.approx(0.7)
    assert v["actual_freq"] == pytest.approx(0.2)
    assert v["direction"] == "overconfident"


def test_calibration_verdict_insufficient():
    assert calibration_verdict([(0.5, 1)] * 3)["direction"] == "insufficient"
