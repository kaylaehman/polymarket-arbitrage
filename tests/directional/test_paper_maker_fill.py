"""Tests for paper maker-order fill modeling in Tracker._check_pending_maker (Task 2).

A resting paper NO-buy at post_price (= entry_price) fills iff the real
market's NO ask reached <= post_price; otherwise it expires "unfilled" at
TTL — never a real trade, and excluded from closed-position P&L/win-rate.
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from core.directional.tracker import Tracker
from core.directional.models import DirectionalPosition
from core.directional.store import DirectionalStore


def _paper_pending_pos(entry=0.90, opened_at=None) -> DirectionalPosition:
    return DirectionalPosition(
        market_id="kalshi:KX-PAPER-1",
        side="NO",
        entry_price=entry,
        size=5,
        strategy="maker_longshot",
        mode="paper",
        opened_at=opened_at or datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=None,
        take_profit=None,
        notional=4.5,
        status="pending",
        order_id=None,
    )


def _make_tracker(store, no_best_ask):
    client = MagicMock()
    ob = MagicMock()
    ob.no.best_ask = no_best_ask
    client.get_orderbook_unified = AsyncMock(return_value=ob)

    class NoKS:
        class state:
            kill_switch_triggered = False

    executor = MagicMock()
    return Tracker(store, kalshi_client=client, executor=executor, risk_manager=NoKS())


@pytest.mark.asyncio
async def test_paper_maker_fills_when_no_ask_at_or_below_post_price(tmp_path):
    """no.best_ask=0.88 <= post_price 0.90 -> position status becomes open."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    p = _paper_pending_pos(entry=0.90, opened_at=datetime(2026, 6, 18, 0, 0, 0))
    store.record_position(p)

    tracker = _make_tracker(store, no_best_ask=0.88)
    await tracker._check_pending_maker(p, now=datetime(2026, 6, 18, 0, 5, 0), order_ttl_minutes=60.0)

    row = store._conn.execute(
        "SELECT status FROM directional_positions WHERE market_id = ?", (p.market_id,)
    ).fetchone()
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_paper_maker_unfilled_when_no_ask_above_post_price_past_ttl(tmp_path):
    """no.best_ask=0.95 > post_price 0.90, age > TTL -> status becomes unfilled (not closed)."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    opened_at = datetime(2026, 6, 18, 0, 0, 0)
    p = _paper_pending_pos(entry=0.90, opened_at=opened_at)
    store.record_position(p)

    tracker = _make_tracker(store, no_best_ask=0.95)
    now = opened_at + timedelta(minutes=61)  # past 60-minute TTL
    await tracker._check_pending_maker(p, now=now, order_ttl_minutes=60.0)

    row = store._conn.execute(
        "SELECT status, realized_pnl FROM directional_positions WHERE market_id = ?", (p.market_id,)
    ).fetchone()
    assert row["status"] == "unfilled"
    assert row["realized_pnl"] is None


@pytest.mark.asyncio
async def test_paper_maker_still_pending_when_no_ask_above_post_price_before_ttl(tmp_path):
    """no.best_ask=0.95 > post_price 0.90, age < TTL -> status remains pending."""
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()
    opened_at = datetime(2026, 6, 18, 0, 0, 0)
    p = _paper_pending_pos(entry=0.90, opened_at=opened_at)
    store.record_position(p)

    tracker = _make_tracker(store, no_best_ask=0.95)
    now = opened_at + timedelta(minutes=10)  # well under 60-minute TTL
    await tracker._check_pending_maker(p, now=now, order_ttl_minutes=60.0)

    row = store._conn.execute(
        "SELECT status FROM directional_positions WHERE market_id = ?", (p.market_id,)
    ).fetchone()
    assert row["status"] == "pending"
