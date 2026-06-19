"""Tests for core/directional/tracker.py — Task 13."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from core.directional.tracker import should_exit, Tracker
from core.directional.models import DirectionalPosition
from core.directional.store import DirectionalStore


def pos(side="YES", entry=0.6, sl=0.42, tp=0.9, strategy="ai_directional", mode="paper"):
    return DirectionalPosition(
        market_id="kalshi:KX-1",
        side=side,
        entry_price=entry,
        size=5,
        strategy=strategy,
        mode=mode,
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=sl,
        take_profit=tp,
        notional=3.0,
    )


# ── should_exit pure-function tests ────────────────────────────────────────────

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


# ── C1: safe_compounder is NOT in _AI_STRATEGIES — sweep does not close it ────

@pytest.mark.asyncio
async def test_safe_compounder_not_swept_at_max_hold(tmp_path):
    """An 80h-old safe_compounder position is NOT closed by sweep (only resolution closes it)."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    # 80 hours old, price triggers stop_loss if checked, but SC is not in _AI_STRATEGIES
    sc_pos = DirectionalPosition(
        market_id="kalshi:KX-SC",
        side="NO",
        entry_price=0.90,
        size=5,
        strategy="safe_compounder",
        mode="live",
        opened_at=datetime(2026, 6, 15, 0, 0, 0),  # 80h before sweep now
        stop_loss=0.70,
        take_profit=0.97,
        notional=4.5,
        status="open",
    )
    store.record_position(sc_pos)

    client = MagicMock()
    # No resolution: market.result is None
    client.get_market = AsyncMock(return_value=MagicMock(result=None))

    executor = MagicMock()
    executor.close_position = AsyncMock()
    executor.place = AsyncMock()

    rm = MagicMock()
    rm.state.kill_switch_triggered = False

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=rm)
    # Sweep at a time 80h after opened_at, with max_hold_hours=72
    await tracker.sweep(now=datetime(2026, 6, 18, 8, 0, 0), max_hold_hours=72)

    # Neither close_position nor place should have been called for SC position
    executor.close_position.assert_not_called()
    executor.place.assert_not_called()

    # Position must remain open
    remaining = store.open_positions()
    assert len(remaining) == 1
    assert remaining[0].market_id == "kalshi:KX-SC"


# ── I1: Kill switch gate — real test with a seeded triggering position ─────────

@pytest.mark.asyncio
async def test_sweep_does_not_close_when_kill_switch(tmp_path):
    """Live sweep skips placing closing orders when kill switch is triggered."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    # Seed a live ai_directional position whose price triggers stop_loss
    live_pos = DirectionalPosition(
        market_id="kalshi:KX-1",
        side="YES",
        entry_price=0.60,
        size=5,
        strategy="ai_directional",
        mode="live",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=0.42,
        take_profit=0.90,
        notional=3.0,
        status="open",
    )
    store.record_position(live_pos)

    # Client: no resolution, but current price below stop_loss
    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    # Price of 0.30 is well below stop_loss of 0.42 → should_exit returns stop_loss
    ob_mock = MagicMock()
    ob_mock.yes.mid_price = 0.30
    client.get_orderbook_unified = AsyncMock(return_value=ob_mock)

    executor = MagicMock()
    executor.close_position = AsyncMock()
    executor.place = AsyncMock()

    class KS:
        class state:
            kill_switch_triggered = True

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=KS())
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))

    # Kill switch is active — no live close should be placed
    executor.close_position.assert_not_called()
    executor.place.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_closes_when_kill_switch_off(tmp_path):
    """Live sweep DOES close a stop-loss position when kill switch is NOT triggered."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    live_pos = DirectionalPosition(
        market_id="kalshi:KX-1",
        side="YES",
        entry_price=0.60,
        size=5,
        strategy="ai_directional",
        mode="live",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=0.42,
        take_profit=0.90,
        notional=3.0,
        status="open",
    )
    store.record_position(live_pos)

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    ob_mock = MagicMock()
    ob_mock.yes.mid_price = 0.30  # below stop_loss → triggers close
    client.get_orderbook_unified = AsyncMock(return_value=ob_mock)

    executor = MagicMock()
    executor.close_position = AsyncMock()

    class NoKS:
        class state:
            kill_switch_triggered = False

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=NoKS())
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))

    # close_position should have been called once (live close)
    executor.close_position.assert_called_once()
    call_kwargs = executor.close_position.call_args
    called_pos = call_kwargs[0][0] if call_kwargs[0] else call_kwargs[1].get("position")
    assert called_pos.market_id == "kalshi:KX-1"


# ── C2: Live close SELLs the same token at the own-space price ────────────────

@pytest.mark.asyncio
async def test_live_close_sells_same_token_at_own_space_price(tmp_path):
    """C2: Executor.close_position called with same side token and own-space price."""
    from polymarket_client.models import OrderSide, TokenType

    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    live_pos = DirectionalPosition(
        market_id="kalshi:KX-2",
        side="YES",
        entry_price=0.65,
        size=10,
        strategy="ai_directional",
        mode="live",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=0.45,
        take_profit=0.85,
        notional=6.5,
        status="open",
    )
    store.record_position(live_pos)

    current_yes_price = 0.40  # below stop_loss, triggers close

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    ob_mock = MagicMock()
    ob_mock.yes.mid_price = current_yes_price
    client.get_orderbook_unified = AsyncMock(return_value=ob_mock)

    # Capture close_position call
    closed_calls = []

    async def mock_close_position(position, price, mode):
        closed_calls.append({"position": position, "price": price, "mode": mode})

    executor = MagicMock()
    executor.close_position = AsyncMock(side_effect=mock_close_position)

    class NoKS:
        class state:
            kill_switch_triggered = False

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=NoKS())
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))

    assert len(closed_calls) == 1
    call = closed_calls[0]
    # Must SELL the YES token (same side as position) at own-space price (YES price)
    assert call["position"].side == "YES"
    assert abs(call["price"] - current_yes_price) < 1e-9
    assert call["mode"] == "live"


@pytest.mark.asyncio
async def test_live_close_sells_no_token_at_no_space_price(tmp_path):
    """C2: NO-side position close uses NO price and NO token."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    live_pos = DirectionalPosition(
        market_id="kalshi:KX-3",
        side="NO",
        entry_price=0.80,
        size=8,
        strategy="ai_directional",
        mode="live",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=0.60,
        take_profit=0.95,
        notional=6.4,
        status="open",
    )
    store.record_position(live_pos)

    current_no_price = 0.55  # below stop_loss

    client = MagicMock()
    client.get_market = AsyncMock(return_value=MagicMock(result=None))
    ob_mock = MagicMock()
    ob_mock.no.mid_price = current_no_price
    client.get_orderbook_unified = AsyncMock(return_value=ob_mock)

    closed_calls = []

    async def mock_close_position(position, price, mode):
        closed_calls.append({"position": position, "price": price, "mode": mode})

    executor = MagicMock()
    executor.close_position = AsyncMock(side_effect=mock_close_position)

    class NoKS:
        class state:
            kill_switch_triggered = False

    tracker = Tracker(store, kalshi_client=client, executor=executor, risk_manager=NoKS())
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))

    assert len(closed_calls) == 1
    call = closed_calls[0]
    assert call["position"].side == "NO"
    assert abs(call["price"] - current_no_price) < 1e-9
    assert call["mode"] == "live"
