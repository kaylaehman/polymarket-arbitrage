"""
backtest/report.py — Aggregate TradeResults and sweep params.
"""
import statistics
from dataclasses import dataclass, field
from typing import Optional

from backtest.simulate import SimParams, TradeResult, simulate_trades


# NO-price bucket boundaries (upper edge exclusive except last)
_BUCKETS = [
    (0.80, 0.85, "0.80-0.85"),
    (0.85, 0.90, "0.85-0.90"),
    (0.90, 0.95, "0.90-0.95"),
    (0.95, 1.01, "0.95+"),
]


def _bucket_label(no_price: float) -> str:
    for lo, hi, label in _BUCKETS:
        if lo <= no_price < hi:
            return label
    return "other"


@dataclass
class AggResult:
    n_trades: int = 0
    win_rate: float = 0.0
    total_pnl_gross: float = 0.0
    total_pnl_net: float = 0.0
    ev_gross: float = 0.0
    ev_net: float = 0.0
    std_net: float = 0.0
    max_loss: float = 0.0
    by_bucket: dict = field(default_factory=dict)
    by_series: dict = field(default_factory=dict)


@dataclass
class SweepRow:
    n: int
    yes_band_lo: float
    yes_band_hi: float
    n_trades: int
    ev_gross: float
    ev_net: float
    win_rate: float


def aggregate(trades: list[TradeResult]) -> AggResult:
    if not trades:
        return AggResult()

    n = len(trades)
    wins = sum(1 for t in trades if t.won)
    total_gross = sum(t.pnl_gross for t in trades)
    total_net = sum(t.pnl_net for t in trades)
    max_loss = min((t.pnl_net for t in trades), default=0.0)
    std = statistics.stdev(t.pnl_net for t in trades) if n > 1 else 0.0

    # Bucket breakdown
    by_bucket: dict[str, dict] = {}
    for t in trades:
        label = _bucket_label(t.entry_price_no)
        if label not in by_bucket:
            by_bucket[label] = {"n": 0, "wins": 0, "total_net": 0.0}
        by_bucket[label]["n"] += 1
        by_bucket[label]["wins"] += int(t.won)
        by_bucket[label]["total_net"] += t.pnl_net
    for label, stats in by_bucket.items():
        stats["ev_net"] = stats["total_net"] / stats["n"]
        stats["win_rate"] = stats["wins"] / stats["n"]

    # Series breakdown
    by_series: dict[str, dict] = {}
    for t in trades:
        s = t.series
        if s not in by_series:
            by_series[s] = {"n": 0, "wins": 0, "total_net": 0.0}
        by_series[s]["n"] += 1
        by_series[s]["wins"] += int(t.won)
        by_series[s]["total_net"] += t.pnl_net
    for s, stats in by_series.items():
        stats["ev_net"] = stats["total_net"] / stats["n"]
        stats["win_rate"] = stats["wins"] / stats["n"]

    return AggResult(
        n_trades=n,
        win_rate=wins / n,
        total_pnl_gross=total_gross,
        total_pnl_net=total_net,
        ev_gross=total_gross / n,
        ev_net=total_net / n,
        std_net=std,
        max_loss=max_loss,
        by_bucket=by_bucket,
        by_series=by_series,
    )


def sweep_params(
    markets_with_candles: list[dict],
    n_values: Optional[list[int]] = None,
    bands: Optional[list[tuple[float, float]]] = None,
) -> list[SweepRow]:
    if n_values is None:
        n_values = [5, 10, 20, 30]
    if bands is None:
        bands = [(0.05, 0.20), (0.05, 0.15), (0.05, 0.10), (0.10, 0.20)]

    rows: list[SweepRow] = []
    for n in n_values:
        for lo, hi in bands:
            params = SimParams(
                entry_days_before_close=n,
                yes_band_lo=lo,
                yes_band_hi=hi,
                min_entry_volume=100.0,
                use_structural_gate=False,
                structural_min=0.0,
            )
            trades = simulate_trades(markets_with_candles, params)
            agg = aggregate(trades)
            rows.append(SweepRow(
                n=n,
                yes_band_lo=lo,
                yes_band_hi=hi,
                n_trades=agg.n_trades,
                ev_gross=agg.ev_gross,
                ev_net=agg.ev_net,
                win_rate=agg.win_rate,
            ))
    return rows


def format_report(agg: AggResult, sweep_rows: list[SweepRow], trades: list[TradeResult]) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("MAKER/LONGSHOT-NO BACKTEST REPORT")
    lines.append("=" * 60)
    lines.append(f"\nSAMPLE: {agg.n_trades} trades (see confidence note below)")
    lines.append(f"Win rate:        {agg.win_rate:.1%}")
    lines.append(f"EV/trade GROSS:  ${agg.ev_gross:+.4f}")
    lines.append(f"EV/trade NET:    ${agg.ev_net:+.4f}")
    lines.append(f"Total P&L gross: ${agg.total_pnl_gross:+.2f}")
    lines.append(f"Total P&L net:   ${agg.total_pnl_net:+.2f}")
    lines.append(f"Std(net P&L):    ${agg.std_net:.4f}")
    lines.append(f"Max single loss: ${agg.max_loss:+.4f}")

    verdict = "POSITIVE (net)" if agg.ev_net > 0 else "NEGATIVE (net)"
    lines.append(f"\nHEADLINE VERDICT: {verdict}")

    lines.append("\n--- BY NO-PRICE BUCKET ---")
    for label in ["0.80-0.85", "0.85-0.90", "0.90-0.95", "0.95+"]:
        b = agg.by_bucket.get(label)
        if b:
            lines.append(
                f"  {label}: n={b['n']}, win={b['win_rate']:.1%}, ev_net=${b['ev_net']:+.4f}"
            )

    lines.append("\n--- BY SERIES ---")
    for s, b in sorted(agg.by_series.items()):
        lines.append(
            f"  {s}: n={b['n']}, win={b['win_rate']:.1%}, ev_net=${b['ev_net']:+.4f}"
        )

    lines.append("\n--- PARAM SWEEP (N x band, net EV, sorted best->worst) ---")
    sorted_rows = sorted(sweep_rows, key=lambda r: r.ev_net, reverse=True)
    best = sorted_rows[0] if sorted_rows else None
    for r in sorted_rows[:12]:
        marker = " <-- BEST" if r is best else ""
        lines.append(
            f"  N={r.n}, band=[{r.yes_band_lo:.2f},{r.yes_band_hi:.2f}]: "
            f"n={r.n_trades}, ev_net=${r.ev_net:+.4f}, win={r.win_rate:.1%}{marker}"
        )

    any_positive = any(r.ev_net > 0 for r in sweep_rows if r.n_trades > 0)
    lines.append(f"\n  Any config +EV net: {'YES' if any_positive else 'NO'}")

    if best:
        live_default = next(
            (r for r in sweep_rows
             if r.n == 30 and abs(r.yes_band_lo - 0.05) < 0.01 and abs(r.yes_band_hi - 0.20) < 0.01),
            None,
        )
        if live_default and live_default.n_trades > 0:
            delta = best.ev_net - live_default.ev_net
            lines.append(
                f"\n  Best config (N={best.n}, [{best.yes_band_lo},{best.yes_band_hi}]) "
                f"vs live defaults (N=30, [0.05,0.20]): delta=${delta:+.4f}/trade"
            )

    lines.append("\n--- CONFIDENCE NOTE ---")
    n = agg.n_trades
    if n < 20:
        lines.append(f"  WARNING: only {n} trades -- results are statistically unreliable.")
        lines.append("  Do NOT change live params based on this sample alone.")
    elif n < 50:
        lines.append(f"  CAUTION: {n} trades -- suggestive but not conclusive.")
    else:
        lines.append(f"  {n} trades -- reasonable sample for macro series; watch for regime drift.")

    lines.append("=" * 60)
    return "\n".join(lines)
