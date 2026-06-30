"""
Directional Validation
======================

Per-strategy validated-edge stats computed from resolved paper positions,
plus a promotion gate ("go live?" becomes data-driven).

This module is REPORT-ONLY. It NEVER flips trading_mode.
All functions are pure/deterministic except build_report, which does a
single read-only query against the directional store.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StrategyStat:
    strategy: str
    resolved: int       # number of closed positions
    wins: int
    win_rate: float     # wins / resolved; 0.0 when resolved == 0
    net_pnl: float      # sum of realized_pnl (net of fees as recorded)
    avg_pnl: float      # net_pnl / resolved; 0.0 when resolved == 0


def strategy_stats(rows) -> dict[str, StrategyStat]:
    """Aggregate closed-position rows into per-strategy StrategyStat objects.

    Args:
        rows: Iterable of (strategy, realized_pnl) pairs for CLOSED positions.

    Returns:
        Mapping of strategy name -> StrategyStat.
    """
    buckets: dict[str, list[float]] = {}
    for row in rows:
        r = tuple(row)
        strategy, pnl = r[0], float(r[1])
        buckets.setdefault(strategy, []).append(pnl)

    result: dict[str, StrategyStat] = {}
    for strategy, pnls in buckets.items():
        resolved = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        net_pnl = sum(pnls)
        result[strategy] = StrategyStat(
            strategy=strategy,
            resolved=resolved,
            wins=wins,
            win_rate=wins / resolved if resolved else 0.0,
            net_pnl=net_pnl,
            avg_pnl=net_pnl / resolved if resolved else 0.0,
        )
    return result


def promotion_status(
    stat: StrategyStat,
    *,
    min_resolved: int = 30,
    min_net_pnl: float = 0.0,
    min_win_rate: float = 0.0,
    min_avg_pnl: float = 0.0,
    riskless_strategies: frozenset[str] = frozenset(),
) -> str:
    """Determine whether a strategy has earned live consideration.

    Gates (all must pass once the sample is large enough):
      - net_pnl   > min_net_pnl
      - avg_pnl  >= min_avg_pnl   (per-trade edge floor — stops one lucky trade from
                                   reading as "ready" on an otherwise flat strategy)
      - win_rate >= min_win_rate  (SKIPPED for strategies named in riskless_strategies:
                                   arb-style strategies win on net edge, not hit-rate)

    Returns:
        "accumulating" — resolved < min_resolved (insufficient sample)
        "failing"      — resolved >= min_resolved AND fails any active gate
        "ready"        — resolved >= min_resolved AND passes all active gates
    """
    if stat.resolved < min_resolved:
        return "accumulating"
    win_rate_ok = stat.strategy in riskless_strategies or stat.win_rate >= min_win_rate
    passes = (
        stat.net_pnl > min_net_pnl
        and stat.avg_pnl >= min_avg_pnl
        and win_rate_ok
    )
    return "ready" if passes else "failing"


_STATUS_RANK = {"ready": 0, "failing": 1, "accumulating": 2}


# Strategies judged on net edge rather than hit-rate (arb-style); exempt from the
# win-rate gate. The directional artist/AI strategies are NOT here on purpose.
RISKLESS_STRATEGIES: frozenset[str] = frozenset({"multi_outcome", "cross_platform_arb", "bundle_arb"})


def build_report(
    store,
    *,
    min_resolved: int = 30,
    min_avg_pnl: float = 0.0,
    min_win_rate: float = 0.0,
    riskless_strategies: frozenset[str] = RISKLESS_STRATEGIES,
) -> str:
    """Query the store for closed positions and return a human-readable report.

    One line per strategy:
        <strategy>: <status> | resolved=N win_rate=X.XX net_pnl=$X.XXXX avg=$X.XXXX

    Sorted: ready first, then failing, then accumulating; ties broken by net_pnl desc.

    Never raises — returns a single-line "no data" message on any error.
    """
    def _status(s: StrategyStat) -> str:
        return promotion_status(
            s,
            min_resolved=min_resolved,
            min_avg_pnl=min_avg_pnl,
            min_win_rate=min_win_rate,
            riskless_strategies=riskless_strategies,
        )

    try:
        rows = store._conn.execute(
            "SELECT strategy, realized_pnl FROM directional_positions WHERE status='closed'"
        ).fetchall()
    except Exception as exc:
        return f"(validation report unavailable: {exc})"

    if not rows:
        return "(no closed positions yet)"

    stats = strategy_stats(rows)

    ranked = sorted(
        stats.values(),
        key=lambda s: (_STATUS_RANK[_status(s)], -s.net_pnl),
    )

    lines = []
    for s in ranked:
        status = _status(s)
        lines.append(
            f"{s.strategy}: {status} | resolved={s.resolved}"
            f" win_rate={s.win_rate:.2f}"
            f" net_pnl=${s.net_pnl:.4f}"
            f" avg=${s.avg_pnl:.4f}"
        )
    return "\n".join(lines)
