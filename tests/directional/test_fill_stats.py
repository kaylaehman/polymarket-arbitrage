# tests/directional/test_fill_stats.py
"""Test fill-rate statistics reporting for maker positions."""
import pytest
from datetime import datetime
from core.directional.store import DirectionalStore
from core.directional.models import DirectionalPosition


def test_maker_fill_stats(tmp_path):
    """Test maker_fill_stats with various position statuses and outcomes."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()

    base_time = datetime(2026, 6, 18, 0, 0, 0)

    # Create 9 maker_longshot positions with different statuses
    # Note: realized_pnl and closed_at are set via update_position after recording
    positions_data = [
        # 2 pending (resting, never filled)
        ("kalshi:KX-1", "NO", 0.1, 100, 10.0, "pending", None, None),
        ("kalshi:KX-2", "NO", 0.15, 50, 7.5, "pending", None, None),
        # 1 open (filled, held)
        ("kalshi:KX-3", "YES", 0.9, 20, 18.0, "open", None, None),
        # 2 closed with realized_pnl > 0 (wins)
        ("kalshi:KX-4", "NO", 0.2, 100, 20.0, "closed", 5.0, None),
        ("kalshi:KX-5", "YES", 0.8, 50, 40.0, "closed", 8.5, None),
        # 1 closed with realized_pnl < 0 (loss)
        ("kalshi:KX-6", "NO", 0.3, 75, 22.5, "closed", -2.0, None),
        # 3 unfilled (never entered the market)
        ("kalshi:KX-7", "NO", 0.05, 200, 10.0, "unfilled", None, None),
        ("kalshi:KX-8", "YES", 0.95, 30, 28.5, "unfilled", None, None),
        ("kalshi:KX-9", "NO", 0.25, 60, 15.0, "unfilled", None, None),
    ]

    for market_id, side, entry_price, size, notional, status, realized_pnl, closed_at in positions_data:
        pos = DirectionalPosition(
            market_id=market_id,
            side=side,
            entry_price=entry_price,
            size=size,
            strategy="maker_longshot",
            mode="paper",
            opened_at=base_time,
            stop_loss=None,
            take_profit=None,
            notional=notional
        )
        s.record_position(pos)
        # Update status and realized_pnl if needed
        update_fields = {"status": status}
        if realized_pnl is not None:
            update_fields["realized_pnl"] = realized_pnl
        if closed_at is not None:
            update_fields["closed_at"] = closed_at
        s.update_position(market_id, **update_fields)

    # Call maker_fill_stats
    stats = s.maker_fill_stats(strategy="maker_longshot")

    # Assert counts
    assert stats["pending"] == 2, f"Expected 2 pending, got {stats['pending']}"
    assert stats["filled_open"] == 1, f"Expected 1 filled_open, got {stats['filled_open']}"
    assert stats["filled_settled"] == 3, f"Expected 3 filled_settled, got {stats['filled_settled']}"
    assert stats["unfilled"] == 3, f"Expected 3 unfilled, got {stats['unfilled']}"

    # Assert fill_rate = (1 + 3) / (1 + 3 + 3) = 4/7 ≈ 0.571428...
    expected_fill_rate = 4 / 7
    assert stats["fill_rate"] == pytest.approx(expected_fill_rate, rel=1e-5), \
        f"Expected fill_rate ≈ {expected_fill_rate}, got {stats['fill_rate']}"

    # Assert win_rate_filled = 2/3 (2 wins out of 3 settled)
    expected_win_rate = 2 / 3
    assert stats["win_rate_filled"] == pytest.approx(expected_win_rate, rel=1e-5), \
        f"Expected win_rate_filled ≈ {expected_win_rate}, got {stats['win_rate_filled']}"


def test_maker_fill_stats_empty(tmp_path):
    """Test maker_fill_stats with no positions."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()

    stats = s.maker_fill_stats(strategy="maker_longshot")

    assert stats["pending"] == 0
    assert stats["filled_open"] == 0
    assert stats["filled_settled"] == 0
    assert stats["unfilled"] == 0
    assert stats["fill_rate"] is None
    assert stats["win_rate_filled"] is None


def test_maker_fill_stats_no_settled(tmp_path):
    """Test maker_fill_stats with filled positions but no settled ones."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()

    base_time = datetime(2026, 6, 18, 0, 0, 0)

    # 1 open, 2 unfilled
    positions_data = [
        ("kalshi:KX-1", "NO", 0.1, 100, 10.0, "open"),
        ("kalshi:KX-2", "NO", 0.05, 200, 10.0, "unfilled"),
        ("kalshi:KX-3", "YES", 0.95, 30, 28.5, "unfilled"),
    ]

    for market_id, side, entry_price, size, notional, status in positions_data:
        pos = DirectionalPosition(
            market_id=market_id,
            side=side,
            entry_price=entry_price,
            size=size,
            strategy="maker_longshot",
            mode="paper",
            opened_at=base_time,
            stop_loss=None,
            take_profit=None,
            notional=notional
        )
        s.record_position(pos)
        s.update_position(market_id, status=status)

    stats = s.maker_fill_stats(strategy="maker_longshot")

    assert stats["pending"] == 0
    assert stats["filled_open"] == 1
    assert stats["filled_settled"] == 0
    assert stats["unfilled"] == 2
    assert stats["fill_rate"] == pytest.approx(1 / 3, rel=1e-5)
    assert stats["win_rate_filled"] is None  # No settled positions
