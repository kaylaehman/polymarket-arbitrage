"""Tests for pm: (Polymarket music) position settlement in Tracker."""
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from core.directional import tracker as tracker_mod
from core.directional.tracker import Tracker
from core.directional.models import DirectionalPosition


def _pos(side="YES", entry=0.30, size=10):
    return DirectionalPosition(
        market_id="pm:12345",
        side=side,
        entry_price=entry,
        size=size,
        strategy="music_paper",
        mode="dry_run",
        opened_at=datetime.now(timezone.utc),
        stop_loss=None,
        take_profit=None,
        notional=entry * size,
        status="open",
        order_id=None,
    )


class _Store:
    def __init__(self):
        self.updates = []

    def update_position(self, market_id, **kw):
        self.updates.append((market_id, kw))


def _tracker(store, gamma_http="HTTP"):
    return Tracker(
        store=store,
        kalshi_client=MagicMock(),
        executor=MagicMock(),
        risk_manager=MagicMock(),
        pmus_client=None,
        gamma_http=gamma_http,
    )


@pytest.mark.asyncio
async def test_pm_yes_position_wins_when_yes_resolves(monkeypatch):
    async def fake_res(http, mid):
        return "yes"

    monkeypatch.setattr(tracker_mod, "gamma_resolution", fake_res, raising=False)
    store = _Store()
    t = _tracker(store)
    # patch alert to a no-op so we don't need real alerting
    t._alert_settled = lambda *a, **k: None
    closed = await t._check_pm_resolution(_pos(side="YES", entry=0.30, size=10))
    assert closed is True
    mid, kw = store.updates[-1]
    assert mid == "pm:12345" and kw["status"] == "closed"
    # settlement_pnl nets the Kalshi entry fee: fee_per_contract(0.30)=0.02/contract
    assert kw["realized_pnl"] == pytest.approx((1.0 - 0.30) * 10 - 0.02 * 10, rel=1e-3)  # YES won, net of fee


@pytest.mark.asyncio
async def test_pm_yes_position_loses_when_no_resolves(monkeypatch):
    async def fake_res(http, mid):
        return "no"

    monkeypatch.setattr(tracker_mod, "gamma_resolution", fake_res, raising=False)
    store = _Store()
    t = _tracker(store)
    t._alert_settled = lambda *a, **k: None
    await t._check_pm_resolution(_pos(side="YES", entry=0.30, size=10))
    # settlement_pnl nets the Kalshi entry fee: fee_per_contract(0.30)=0.02/contract
    assert store.updates[-1][1]["realized_pnl"] == pytest.approx((0.0 - 0.30) * 10 - 0.02 * 10, rel=1e-3)


@pytest.mark.asyncio
async def test_pm_unresolved_returns_false(monkeypatch):
    async def fake_res(http, mid):
        return None

    monkeypatch.setattr(tracker_mod, "gamma_resolution", fake_res, raising=False)
    store = _Store()
    t = _tracker(store)
    t._alert_settled = lambda *a, **k: None
    assert await t._check_pm_resolution(_pos()) is False
    assert store.updates == []


@pytest.mark.asyncio
async def test_pm_no_http_returns_false():
    store = _Store()
    t = _tracker(store, gamma_http=None)
    t._alert_settled = lambda *a, **k: None
    assert await t._check_pm_resolution(_pos()) is False


@pytest.mark.asyncio
async def test_check_resolution_routes_pm(monkeypatch):
    async def fake_res(http, mid):
        return "yes"

    monkeypatch.setattr(tracker_mod, "gamma_resolution", fake_res, raising=False)
    store = _Store()
    t = _tracker(store)
    t._alert_settled = lambda *a, **k: None
    assert await t._check_resolution(_pos(side="YES")) is True  # routed to pm path
