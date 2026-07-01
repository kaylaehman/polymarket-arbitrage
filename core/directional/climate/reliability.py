"""Climate calibration / reliability report.

Reads (predicted p_yes, actual outcome) pairs logged on settlement into the
``climate_calibration`` table and reports whether the model is well-calibrated:
- reliability bins: predicted-probability decile vs actual frequency,
- Brier score (lower is better; 0 = perfect),
- a plain-language verdict (overconfident / underconfident / calibrated),

so a too-wide forecast sigma shows up as systematic over/under-confidence and can
be tightened. Pure functions + a store reader; never raises from the reader.
"""
from __future__ import annotations

from typing import Iterable, Optional

# Below this many resolved predictions, don't trust a calibration verdict.
_MIN_SAMPLE = 15
# How far predicted mean can sit from actual frequency before we call it biased.
_TOLERANCE = 0.08


def reliability_bins(rows: Iterable[tuple], n_bins: int = 10) -> list[dict]:
    """Bin (p_yes, outcome_yes) pairs by predicted probability.

    Returns one dict per non-empty bin: {lo, hi, n, pred_mean, actual_freq},
    sorted by bin. A well-calibrated model has pred_mean ≈ actual_freq in each bin.
    """
    buckets: dict[int, list] = {}
    for p, o in rows:
        p = float(p)
        o = int(o)
        b = min(int(p * n_bins), n_bins - 1)
        d = buckets.setdefault(b, [0, 0.0, 0])  # [count, sum_pred, sum_outcome]
        d[0] += 1
        d[1] += p
        d[2] += o
    out = []
    for b in sorted(buckets):
        cnt, sp, so = buckets[b]
        out.append({
            "lo": round(b / n_bins, 4),
            "hi": round((b + 1) / n_bins, 4),
            "n": cnt,
            "pred_mean": round(sp / cnt, 4),
            "actual_freq": round(so / cnt, 4),
        })
    return out


def brier_score(rows: Iterable[tuple]) -> Optional[float]:
    """Mean squared error of predicted p_yes vs {0,1} outcome. None if empty."""
    rows = list(rows)
    if not rows:
        return None
    return round(sum((float(p) - int(o)) ** 2 for p, o in rows) / len(rows), 4)


def calibration_verdict(rows: Iterable[tuple]) -> dict:
    """Overall verdict: is the model over/under-confident on average?

    Returns {n, pred_mean, actual_freq, brier, direction} where direction is
    "overconfident" (predicts higher than reality), "underconfident" (lower),
    "calibrated" (within tolerance), or "insufficient" (< _MIN_SAMPLE).
    """
    rows = list(rows)
    n = len(rows)
    out = {"n": n, "pred_mean": None, "actual_freq": None,
           "brier": brier_score(rows), "direction": "insufficient"}
    if n == 0:
        return out
    pred = sum(float(p) for p, _ in rows) / n
    actual = sum(int(o) for _, o in rows) / n
    out["pred_mean"] = round(pred, 4)
    out["actual_freq"] = round(actual, 4)
    if n < _MIN_SAMPLE:
        return out
    if pred - actual > _TOLERANCE:
        out["direction"] = "overconfident"
    elif actual - pred > _TOLERANCE:
        out["direction"] = "underconfident"
    else:
        out["direction"] = "calibrated"
    return out


def _family_of(market_id: str) -> str:
    """Coarse climate family from a market_id (for per-family breakdown)."""
    t = market_id.split(":", 1)[-1].upper()
    if t.startswith("KXTEMP"):
        return "hourly_temp"
    if t.startswith("KXLOW"):
        return "low_temp"
    if "HIGH" in t:
        return "high_temp"
    return "other"


def climate_reliability(store) -> dict:
    """Read climate_calibration and return {family: verdict+bins}, plus 'overall'.

    Never raises — returns {} if the table is missing/unreadable.
    """
    try:
        rows = store._conn.execute(
            "SELECT market_id, p_yes, outcome_yes FROM climate_calibration"
        ).fetchall()
    except Exception:
        return {}

    by_family: dict[str, list] = {"overall": []}
    for r in rows:
        pair = (r["p_yes"], r["outcome_yes"])
        by_family["overall"].append(pair)
        by_family.setdefault(_family_of(r["market_id"]), []).append(pair)

    report = {}
    for fam, pairs in by_family.items():
        v = calibration_verdict(pairs)
        v["bins"] = reliability_bins(pairs)
        report[fam] = v
    return report


def format_report(report: dict) -> str:
    """Human-readable reliability report (for a CLI / digest)."""
    if not report:
        return "No climate calibration data yet (positions settle -> rows accrue)."
    lines = ["Climate calibration (predicted P(YES) vs actual frequency):"]
    for fam in sorted(report):
        v = report[fam]
        if v["pred_mean"] is None:
            lines.append(f"  {fam}: n=0")
            continue
        lines.append(
            f"  {fam}: n={v['n']} pred={v['pred_mean']:.3f} actual={v['actual_freq']:.3f}"
            f" brier={v['brier']} -> {v['direction'].upper()}"
        )
        for b in v["bins"]:
            lines.append(
                f"      [{b['lo']:.2f}-{b['hi']:.2f}) n={b['n']:<3}"
                f" pred={b['pred_mean']:.3f} actual={b['actual_freq']:.3f}"
            )
    return "\n".join(lines)
