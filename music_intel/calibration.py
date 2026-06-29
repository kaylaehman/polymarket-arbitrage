"""Phase 3 — calibration & backtest harness.

Replays historical kworb snapshots against ACTUAL Billboard results (the only
place Billboard is consulted) to measure Brier score and build a calibration
curve. The live model applies the curve. Always reports OUT-OF-SAMPLE results
(train the curve on one split, evaluate Brier on the held-out split) so the
heuristic is never graded on data it was tuned against.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional


def brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """Mean squared error of probabilistic predictions vs binary outcomes (0..1,
    lower is better). Empty -> 1.0 (worst, uninformative)."""
    if not predictions:
        return 1.0
    return sum((p - o) ** 2 for p, o in zip(predictions, outcomes)) / len(predictions)


def calibration_curve(predictions: list[float], outcomes: list[int], bins: int = 10) -> list[dict]:
    """Bin predicted probabilities; report observed frequency per bin.

    Returns [{bin_lo, bin_hi, mean_pred, observed_freq, n}, ...] for non-empty bins.
    A well-calibrated model has observed_freq ≈ mean_pred.
    """
    acc: dict[int, list] = {}
    for p, o in zip(predictions, outcomes):
        idx = min(int(p * bins), bins - 1)
        acc.setdefault(idx, []).append((p, o))
    curve = []
    for idx in sorted(acc):
        ps = [p for p, _ in acc[idx]]
        os = [o for _, o in acc[idx]]
        curve.append({
            "bin_lo": round(idx / bins, 4), "bin_hi": round((idx + 1) / bins, 4),
            "mean_pred": round(sum(ps) / len(ps), 4),
            "observed_freq": round(sum(os) / len(os), 4), "n": len(os),
        })
    return curve


def apply_calibration(prob: float, curve: list[dict]) -> float:
    """Map a raw probability through a calibration curve (bin lookup -> observed
    frequency). Falls back to the raw prob when the curve has no covering bin."""
    if not curve:
        return prob
    for b in curve:
        if b["bin_lo"] <= prob < b["bin_hi"] or (prob >= 1.0 and b["bin_hi"] >= 1.0):
            return b["observed_freq"]
    return prob


@dataclass
class BacktestResult:
    n: int
    brier_out_of_sample: float
    brier_in_sample: float
    calibration: list           # curve fit on the TRAIN split
    predictions: list           # (week, predicted_prob, outcome) for inspection


def backtest(
    weeks: list,                # list of (week_key, kworb_records, actual_number_one_key)
    project_fn: Callable,       # (records) -> raw P(#1 for actual leader's question)
    *,
    train_frac: float = 0.5,
    bins: int = 10,
) -> BacktestResult:
    """Replay each historical week: project from kworb, compare to the actual
    Billboard #1. Fit the calibration curve on the TRAIN split, report Brier on
    the held-out TEST split (out-of-sample). `project_fn(records)` returns
    (predicted_prob, predicted_target_key)."""
    rows = []
    for week_key, records, actual_no1 in weeks:
        pred_prob, pred_key = project_fn(records)
        outcome = 1 if pred_key == actual_no1 else 0
        rows.append((week_key, float(pred_prob), int(outcome)))

    split = max(1, int(len(rows) * train_frac))
    train, test = rows[:split], rows[split:] or rows[:split]
    train_curve = calibration_curve([p for _, p, _ in train], [o for _, _, o in train], bins=bins)
    test_pred = [apply_calibration(p, train_curve) for _, p, _ in test]
    test_out = [o for _, _, o in test]
    return BacktestResult(
        n=len(rows),
        brier_out_of_sample=round(brier_score(test_pred, test_out), 4),
        brier_in_sample=round(brier_score([p for _, p, _ in train], [o for _, _, o in train]), 4),
        calibration=train_curve, predictions=rows,
    )
