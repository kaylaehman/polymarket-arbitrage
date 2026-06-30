"""C1/C2: the dashboard needs per-mode P&L/exposure so the top cards are accurate
and a paper-vs-actual toggle is possible. pnl_summary_by_mode splits the aggregate
by the position 'mode' column.
"""
import pytest
from datetime import datetime
from core.directional.store import DirectionalStore
from core.directional.models import DirectionalPosition


def _pos(mid, mode, notional, status="open", realized=None):
    p = DirectionalPosition(
        market_id=mid, side="NO", entry_price=0.9, size=5,
        strategy="maker_longshot", mode=mode,
        opened_at=datetime(2026, 6, 18), stop_loss=None, take_profit=None,
        notional=notional,
    )
    return p


def _store(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    return s


def test_pnl_summary_by_mode_splits_paper_and_live(tmp_path):
    s = _store(tmp_path)
    # 2 open paper ($4.5 + $7.0), 1 closed paper (+$2), 1 open live ($3), 1 closed live (-$1)
    s.record_position(_pos("kalshi:P1", "paper", 4.5))
    s.record_position(_pos("kalshi:P2", "paper", 7.0))
    s.record_position(_pos("kalshi:P3", "paper", 5.0))
    s.update_position("kalshi:P3", status="closed", realized_pnl=2.0)
    s.record_position(_pos("kalshi:L1", "live", 3.0))
    s.record_position(_pos("kalshi:L2", "live", 6.0))
    s.update_position("kalshi:L2", status="closed", realized_pnl=-1.0)

    by_mode = s.pnl_summary_by_mode()

    paper = by_mode["paper"]
    assert paper["open_count"] == 2
    assert paper["open_exposure"] == pytest.approx(11.5)
    assert paper["closed_count"] == 1
    assert paper["total_realized_pnl"] == pytest.approx(2.0)

    live = by_mode["live"]
    assert live["open_count"] == 1
    assert live["open_exposure"] == pytest.approx(3.0)
    assert live["closed_count"] == 1
    assert live["total_realized_pnl"] == pytest.approx(-1.0)


def test_pnl_summary_by_mode_empty(tmp_path):
    s = _store(tmp_path)
    assert s.pnl_summary_by_mode() == {}


def test_pnl_summary_by_mode_paper_only(tmp_path):
    s = _store(tmp_path)
    s.record_position(_pos("kalshi:P1", "paper", 4.5))
    by_mode = s.pnl_summary_by_mode()
    assert set(by_mode.keys()) == {"paper"}
    assert by_mode["paper"]["open_exposure"] == pytest.approx(4.5)
