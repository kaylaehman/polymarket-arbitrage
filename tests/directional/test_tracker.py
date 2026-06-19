"""Tests for core/directional/tracker.py — Task 13."""
import pytest
from datetime import datetime
from core.directional.tracker import should_exit
from core.directional.models import DirectionalPosition


def pos(side="YES", entry=0.6, sl=0.42, tp=0.9):
    return DirectionalPosition(
        market_id="kalshi:KX-1",
        side=side,
        entry_price=entry,
        size=5,
        strategy="ai_directional",
        mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=sl,
        take_profit=tp,
        notional=3.0,
    )


def test_stop_loss_triggers():
    ok, why = should_exit(pos(), price=0.40, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "stop_loss"


def test_take_profit_triggers():
    ok, why = should_exit(pos(), price=0.92, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "take_profit"


def test_time_exit():
    ok, why = should_exit(pos(), price=0.6, now=datetime(2026, 6, 22, 0, 0, 0), max_hold_hours=72)
    assert ok and why == "max_hold"


def test_hold_otherwise():
    ok, _ = should_exit(pos(), price=0.6, now=datetime(2026, 6, 18, 2, 0, 0), max_hold_hours=72)
    assert ok is False


def test_no_side_should_exit():
    """NO position: stop_loss stored as NO-price; same logic applies."""
    p = pos(side="NO", entry=0.9, sl=0.70, tp=0.97)  # NO price sl=0.70, tp=0.97
    ok, why = should_exit(p, price=0.68, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "stop_loss"
    ok2, why2 = should_exit(p, price=0.98, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok2 and why2 == "take_profit"


@pytest.mark.asyncio
async def test_sweep_does_not_close_when_kill_switch(tmp_path):
    """Live sweep skips placing closing orders when kill switch is triggered."""
    from unittest.mock import AsyncMock, MagicMock
    from core.directional.tracker import Tracker
    from core.directional.store import DirectionalStore

    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    class KS:
        class state:
            kill_switch_triggered = True

    executor = MagicMock()
    executor.place = AsyncMock()
    tracker = Tracker(store, kalshi_client=MagicMock(), executor=executor, risk_manager=KS())
    # Even if there are open positions, live close should not be called
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))
    executor.place.assert_not_called()
