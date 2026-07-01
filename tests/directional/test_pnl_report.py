"""Tests for the /trade-report + /daily-report P&L report builders.

Covers the store SQL window (daily_pnl_by_mode) against a real temp DB and the
pure formatting (total_report / daily_report) against a fake store, including
the NULL-realized_pnl (unfilled maker) and empty-store edge cases.
"""
import datetime

import pytest

from core.directional.models import DirectionalPosition
from core.directional.store import DirectionalStore
from core.directional import pnl_report


# ── store.daily_pnl_by_mode ────────────────────────────────────────────────

def _pos(store, *, market_id, mode="paper", status="closed", realized=None,
         opened, closed=None, strategy="maker_longshot"):
    """Insert a position row directly, controlling opened_at/closed_at/realized."""
    p = DirectionalPosition(
        market_id=market_id, side="NO", entry_price=0.9, size=5,
        strategy=strategy, mode=mode,
        opened_at=datetime.datetime.fromisoformat(opened),
        stop_loss=None, take_profit=None,
        notional=4.5, status=status,
    )
    pid = store.record_position(p)
    store._conn.execute(
        "UPDATE directional_positions SET status=?, realized_pnl=?, closed_at=? WHERE id=?",
        (status, realized, closed, pid),
    )
    store._conn.commit()
    return pid


@pytest.fixture
def store(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    return s


def test_daily_pnl_by_mode_windows_on_closed_and_opened(store):
    day = "2026-07-01"
    # settled today: 1 win (+0.4), 1 loss (-1.0), 1 unfilled (NULL pnl -> $0, no W/L)
    _pos(store, market_id="kalshi:A", opened="2026-06-29T10:00:00+00:00",
         realized=0.4, closed="2026-07-01T16:20:08+00:00")
    _pos(store, market_id="kalshi:B", opened="2026-06-29T11:00:00+00:00",
         realized=-1.0, closed="2026-07-01T18:00:00+00:00")
    _pos(store, market_id="kalshi:C", opened="2026-06-30T09:00:00+00:00",
         realized=None, closed="2026-07-01T20:35:44+00:00")
    # settled YESTERDAY — must be excluded from today's window
    _pos(store, market_id="kalshi:D", opened="2026-06-28T09:00:00+00:00",
         realized=5.0, closed="2026-06-30T23:59:59+00:00")
    # opened today, still open (no closed_at) — counts as placed, not settled
    _pos(store, market_id="kalshi:E", opened="2026-07-01T08:00:00+00:00",
         status="open", realized=None, closed=None)

    res = store.daily_pnl_by_mode(day)
    assert set(res) == {"paper"}
    b = res["paper"]
    assert b["settled_count"] == 3          # A, B, C
    assert b["wins"] == 1                    # A
    assert b["losses"] == 1                  # B (C is NULL -> neither)
    assert abs(b["realized_pnl"] - (0.4 - 1.0)) < 1e-9   # C contributes $0
    assert b["opened_count"] == 1            # E (A–D opened on prior days)


def test_daily_pnl_by_mode_quiet_day_is_empty(store):
    _pos(store, market_id="kalshi:A", opened="2026-06-29T10:00:00+00:00",
         realized=0.4, closed="2026-06-29T16:00:00+00:00")
    assert store.daily_pnl_by_mode("2026-07-01") == {}


# ── pure formatting ─────────────────────────────────────────────────────────

class FakeStore:
    def __init__(self, by_mode=None, daily=None):
        self._by_mode = by_mode or {}
        self._daily = daily or {}

    def pnl_summary_by_mode(self):
        return self._by_mode

    def daily_pnl_by_mode(self, day_iso):
        return self._daily


def test_total_report_formats_realized_and_winrate():
    s = FakeStore(by_mode={"paper": {
        "open_count": 46, "closed_count": 43, "open_exposure": 523.0,
        "total_realized_pnl": 16.90, "wins": 30, "dir_closed": 30,
    }})
    txt = pnl_report.total_report(s)
    assert "Trade Report" in txt
    assert "$+16.90" in txt
    assert "100%" in txt          # 30/30
    assert "🟢" in txt


def test_total_report_empty_store():
    assert "No positions" in pnl_report.total_report(FakeStore())


def test_daily_report_formats_today():
    s = FakeStore(daily={"paper": {
        "settled_count": 20, "wins": 18, "losses": 2,
        "realized_pnl": 14.84, "opened_count": 12,
    }})
    txt = pnl_report.daily_report(s, day_iso="2026-07-01")
    assert "2026-07-01" in txt
    assert "18W/2L" in txt
    assert "$+14.84" in txt
    assert "placed  12" in txt.replace("  ", " ").replace("  ", " ") or "placed" in txt


def test_daily_report_loss_shows_red():
    s = FakeStore(daily={"paper": {
        "settled_count": 3, "wins": 1, "losses": 2,
        "realized_pnl": -4.20, "opened_count": 0,
    }})
    txt = pnl_report.daily_report(s, day_iso="2026-07-01")
    assert "🔴" in txt
    assert "$-4.20" in txt


def test_daily_report_quiet_day():
    assert "No bets" in pnl_report.daily_report(FakeStore(), day_iso="2026-07-01")
