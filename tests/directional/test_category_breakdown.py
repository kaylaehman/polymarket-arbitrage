# tests/directional/test_category_breakdown.py
"""Tests for per-category validation breakout (improvement #1).

The breakout turns the accumulating paper trades into a per-category verdict
(win-rate + net EV + resolved-sample count) instead of one aggregate number
dominated by weather.
"""
import pytest
from datetime import datetime

from core.directional.store import (
    DirectionalStore,
    category_for_market_id,
    VALIDATION_MIN_SAMPLE,
)
from core.directional.models import DirectionalPosition


def _pos(market_id, entry=0.93, size=8, status="open", strategy="maker_longshot"):
    return DirectionalPosition(
        market_id=market_id, side="NO", entry_price=entry, size=size,
        strategy=strategy, mode="paper",
        opened_at=datetime(2026, 6, 23, 0, 0, 0),
        stop_loss=None, take_profit=None, notional=entry * size,
    )


# ── category_for_market_id (pure ticker → category mapping) ──────────────────

@pytest.mark.parametrize("market_id,expected", [
    ("kalshi:KXHIGHNY-26JUN23-B78.5", "weather"),
    ("kalshi:KXHIGHLAX-26JUN25-B74.5", "weather"),
    ("kalshi:KXHIGHAUS-26JUL01-B98.5", "weather"),
    ("pmus:tc-temp-sfohigh-2026-06-24-gte74f", "weather"),
    ("kalshi:KXCPIYOY-26JUN-T3.9", "macro"),
    ("kalshi:KXCPICORE-26JUN-T0.3", "macro"),
    ("kalshi:KXPCECORE-26MAY-T0.4", "macro"),
    ("kalshi:KXFEDDECISION-26JUL", "macro"),
    ("kalshi:KXGDP-26Q2-T2.5", "macro"),
    ("kalshi:KXBTCD-26JUN24-T100000", "financial"),
    ("kalshi:KXETH-26JUN-B3000", "financial"),
    ("kalshi:KXWTI-26JUL-T80", "financial"),
    ("kalshi:KXCABLEAVE-26MAY22-26JUL", "media"),
    ("kalshi:KXNBA-26-BOS", "sports"),
    ("kalshi:KXNHL-26-EDM", "sports"),
    ("kalshi:KXSOMETHINGELSE-26", "other"),
])
def test_category_for_market_id(market_id, expected):
    assert category_for_market_id(market_id) == expected


# ── category_breakdown aggregation ──────────────────────────────────────────

def test_breakdown_empty(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    assert s.category_breakdown() == {}


def test_breakdown_open_only(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    s.record_position(_pos("kalshi:KXHIGHNY-26JUN23-B78.5"))
    s.record_position(_pos("kalshi:KXCPIYOY-26JUN-T3.9"))
    bd = s.category_breakdown()
    assert set(bd) == {"weather", "macro"}
    w = bd["weather"]
    assert w["open_count"] == 1
    assert w["closed_count"] == 0
    assert w["wins"] == 0
    assert w["win_rate"] is None          # undefined with 0 resolved
    assert w["realized_pnl"] == pytest.approx(0.0)
    assert w["avg_pnl_per_trade"] is None
    assert w["open_exposure"] == pytest.approx(0.93 * 8)


def test_breakdown_win_and_loss(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    # weather: one win (+0.56), one loss (-7.44)
    s.record_position(_pos("kalshi:KXHIGHNY-26JUN22-B74.5"))
    s.update_position("kalshi:KXHIGHNY-26JUN22-B74.5", status="closed", realized_pnl=0.56)
    s.record_position(_pos("kalshi:KXHIGHLAX-26JUN22-B69.5"))
    s.update_position("kalshi:KXHIGHLAX-26JUN22-B69.5", status="closed", realized_pnl=-7.44)
    # macro: one open
    s.record_position(_pos("kalshi:KXCPIYOY-26JUN-T3.9"))

    bd = s.category_breakdown()
    w = bd["weather"]
    assert w["closed_count"] == 2
    assert w["wins"] == 1
    assert w["losses"] == 1
    assert w["win_rate"] == pytest.approx(0.5)
    assert w["realized_pnl"] == pytest.approx(0.56 - 7.44)
    assert w["avg_pnl_per_trade"] == pytest.approx((0.56 - 7.44) / 2)
    assert w["open_count"] == 0

    m = bd["macro"]
    assert m["closed_count"] == 0
    assert m["open_count"] == 1


def test_breakdown_excludes_multi_outcome_strategy(tmp_path):
    """Riskless multi-outcome locks (N YES legs, 1 wins) must NOT pollute the
    longshot-NO validation breakdown — they live in their own summary."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    # one real maker NO bet (weather)
    s.record_position(_pos("kalshi:KXHIGHNY-26JUN22-B74.5"))
    s.update_position("kalshi:KXHIGHNY-26JUN22-B74.5", status="closed", realized_pnl=0.56)
    # a multi_outcome lock recorded as YES legs on the same weather category
    for leg in ("B79.5", "B81.5", "B83.5"):
        mid = f"kalshi:KXHIGHNY-26JUN25-{leg}"
        s.record_position(_pos(mid, strategy="multi_outcome"))
    bd = s.category_breakdown()
    # weather reflects ONLY the maker bet, not the 3 multi_outcome legs
    assert bd["weather"]["closed_count"] == 1
    assert bd["weather"]["open_count"] == 0


def test_multi_outcome_summary(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    assert s.multi_outcome_summary() == {
        "open_count": 0, "closed_count": 0, "realized_pnl": 0.0, "open_notional": 0.0}
    # record a 3-leg lock, then resolve: one wins (+), two lose (-)
    legs = [("kalshi:KXFEDDECISION-26JUL-H", 0.30),
            ("kalshi:KXFEDDECISION-26JUL-C25", 0.30),
            ("kalshi:KXFEDDECISION-26JUL-C50", 0.25)]
    for mid, px in legs:
        p = _pos(mid, entry=px, status="open", strategy="multi_outcome")
        p.side = "YES"
        s.record_position(p)
    summ = s.multi_outcome_summary()
    assert summ["open_count"] == 3
    assert summ["open_notional"] == pytest.approx((0.30 + 0.30 + 0.25) * 8)


# ── statistical gate / verdict (#3) ─────────────────────────────────────────

def _record_closed(s, n, pnl, series="KXHIGHNY", base=0):
    for i in range(n):
        mid = f"kalshi:{series}-26JUN{base + i:04d}-B70.5"
        s.record_position(_pos(mid))
        s.update_position(mid, status="closed", realized_pnl=pnl(i) if callable(pnl) else pnl)


def test_verdict_insufficient_below_min_sample(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    _record_closed(s, 5, 0.50)
    w = s.category_breakdown()["weather"]
    assert w["verdict"] == "insufficient"
    assert w["needed_samples"] == VALIDATION_MIN_SAMPLE - 5


def test_verdict_positive_when_ci_above_zero(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    # min_sample wins of +0.50 each, tiny variance → 90% lower bound > 0
    _record_closed(s, VALIDATION_MIN_SAMPLE, lambda i: 0.50 + (0.001 if i % 2 else -0.001))
    w = s.category_breakdown()["weather"]
    assert w["closed_count"] == VALIDATION_MIN_SAMPLE
    assert w["needed_samples"] == 0
    assert w["verdict"] == "positive"
    assert w["ev_ci90_lo"] > 0


def test_verdict_inconclusive_when_ci_straddles_zero(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    # big alternating swings → mean ~0, wide CI spanning zero
    _record_closed(s, VALIDATION_MIN_SAMPLE, lambda i: 5.0 if i % 2 else -5.0)
    w = s.category_breakdown()["weather"]
    assert w["verdict"] == "inconclusive"
    assert w["ev_ci90_lo"] < 0 < w["ev_ci90_hi"]


def test_breakdown_win_rate_all_wins(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    for i in range(3):
        mid = f"kalshi:KXHIGHNY-26JUN2{i}-B70.5"
        s.record_position(_pos(mid))
        s.update_position(mid, status="closed", realized_pnl=0.5)
    w = s.category_breakdown()["weather"]
    assert w["closed_count"] == 3
    assert w["wins"] == 3
    assert w["win_rate"] == pytest.approx(1.0)
    assert w["realized_pnl"] == pytest.approx(1.5)
