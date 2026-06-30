import pytest
from core.directional.validation import (
    strategy_stats, promotion_status, build_report, StrategyStat,
)


def test_strategy_stats_aggregates():
    rows = [("maker_longshot", 0.5), ("maker_longshot", -0.2), ("maker_longshot", 0.3),
            ("artist_paper", -1.0)]
    st = strategy_stats(rows)
    ml = st["maker_longshot"]
    assert ml.resolved == 3 and ml.wins == 2
    assert ml.win_rate == pytest.approx(2/3)
    assert ml.net_pnl == pytest.approx(0.6)
    assert ml.avg_pnl == pytest.approx(0.2)
    assert st["artist_paper"].net_pnl == pytest.approx(-1.0) and st["artist_paper"].wins == 0


def test_promotion_status_accumulating():
    s = StrategyStat("x", resolved=5, wins=4, win_rate=0.8, net_pnl=2.0, avg_pnl=0.4)
    assert promotion_status(s, min_resolved=30) == "accumulating"


def test_promotion_status_ready():
    s = StrategyStat("x", resolved=40, wins=24, win_rate=0.6, net_pnl=5.0, avg_pnl=0.125)
    assert promotion_status(s, min_resolved=30, min_net_pnl=0.0, min_win_rate=0.5) == "ready"


def test_promotion_status_failing_negative_pnl():
    s = StrategyStat("x", resolved=40, wins=10, win_rate=0.25, net_pnl=-3.0, avg_pnl=-0.075)
    assert promotion_status(s, min_resolved=30) == "failing"


def test_promotion_status_failing_low_winrate():
    s = StrategyStat("x", resolved=40, wins=12, win_rate=0.30, net_pnl=1.0, avg_pnl=0.025)
    assert promotion_status(s, min_resolved=30, min_net_pnl=0.0, min_win_rate=0.5) == "failing"


def test_build_report_from_fake_store():
    class _Conn:
        def execute(self, *a, **k):
            class _C:
                def fetchall(self_inner): return [
                    ("maker_longshot", 0.5), ("maker_longshot", 0.3), ("maker_longshot", -0.1),
                    ("artist_paper", -1.0),
                ]
            return _C()
    class _Store: _conn = _Conn()
    report = build_report(_Store(), min_resolved=2)
    assert "maker_longshot" in report and "artist_paper" in report
    # maker_longshot resolved=3 net +0.7 -> ready (>=2, net>0, win_rate .67>=0); appears before failing artist_paper
    assert report.index("maker_longshot") < report.index("artist_paper")


# --- M2: stronger gate (min_avg_pnl) + riskless-strategy win-rate exemption ---

def test_promotion_status_failing_when_avg_pnl_below_floor():
    # Net positive but razor-thin per-trade edge (one lucky trade): should FAIL a min_avg_pnl floor.
    s = StrategyStat("x", resolved=40, wins=21, win_rate=0.525, net_pnl=0.40, avg_pnl=0.01)
    assert promotion_status(s, min_resolved=30, min_avg_pnl=0.05) == "failing"
    # Without the floor it would have read "ready" (net>0, win_rate ok) — confirm the floor is what fails it.
    assert promotion_status(s, min_resolved=30, min_avg_pnl=0.0) == "ready"


def test_promotion_status_ready_when_avg_pnl_above_floor():
    s = StrategyStat("x", resolved=40, wins=26, win_rate=0.65, net_pnl=4.0, avg_pnl=0.10)
    assert promotion_status(s, min_resolved=30, min_avg_pnl=0.05, min_win_rate=0.5) == "ready"


def test_riskless_strategy_exempt_from_winrate_floor():
    # An arb-style strategy with a low win rate but solid net/avg PnL: the win-rate
    # gate should NOT fail it when it's named riskless.
    s = StrategyStat("multi_outcome", resolved=40, wins=12, win_rate=0.30, net_pnl=3.0, avg_pnl=0.075)
    assert promotion_status(s, min_resolved=30, min_win_rate=0.5) == "failing"
    assert promotion_status(
        s, min_resolved=30, min_win_rate=0.5, riskless_strategies=frozenset({"multi_outcome"})
    ) == "ready"


def test_riskless_strategy_still_needs_positive_pnl():
    # Exemption is ONLY from the win-rate gate — a losing arb strategy still fails.
    s = StrategyStat("multi_outcome", resolved=40, wins=8, win_rate=0.2, net_pnl=-2.0, avg_pnl=-0.05)
    assert promotion_status(
        s, min_resolved=30, min_win_rate=0.5, riskless_strategies=frozenset({"multi_outcome"})
    ) == "failing"
