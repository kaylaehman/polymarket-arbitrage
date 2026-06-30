"""Unrealized mark-to-market for the dashboard top cards.

compute_unrealized_by_mode marks each OPEN position to market via an injected
async price function and aggregates per mode, returning unrealized P&L AND
coverage (marked/total) so a partial mark is never silently misleading.
"""
import pytest
from dashboard.server import compute_unrealized_by_mode


class _P:
    def __init__(self, market_id, side, entry_price, size, mode):
        self.market_id = market_id
        self.side = side
        self.entry_price = entry_price
        self.size = size
        self.mode = mode


@pytest.mark.asyncio
async def test_unrealized_aggregates_by_mode_with_coverage():
    positions = [
        _P("pm:1", "NO", 0.165, 48, "paper"),     # NO entered cheap; yes falls -> gain
        _P("kalshi:A", "NO", 0.95, 8, "paper"),    # NO at 0.95; yes drops -> small loss
        _P("kalshi:B", "YES", 0.50, 10, "live"),   # live bucket
    ]
    prices = {"pm:1": 0.70, "kalshi:A": 0.10, "kalshi:B": 0.60}

    async def price_fn(mid):
        return prices.get(mid)

    out = await compute_unrealized_by_mode(positions, price_fn)
    # paper: pm:1 NO -> (0.30-0.165)*48 = 6.48 ; kalshi:A NO -> (0.90-0.95)*8 = -0.40 ; sum 6.08
    assert out["paper"]["unrealized"] == pytest.approx(6.08, abs=1e-4)
    assert out["paper"]["marked"] == 2 and out["paper"]["total"] == 2
    assert out["live"]["unrealized"] == pytest.approx(1.0, abs=1e-4)
    assert out["live"]["marked"] == 1 and out["live"]["total"] == 1


@pytest.mark.asyncio
async def test_unrealized_reports_partial_coverage():
    positions = [_P("pm:1", "NO", 0.165, 48, "paper"), _P("pm:2", "YES", 0.5, 10, "paper")]

    async def price_fn(mid):
        return 0.70 if mid == "pm:1" else None   # pm:2 price unavailable

    out = await compute_unrealized_by_mode(positions, price_fn)
    assert out["paper"]["unrealized"] == pytest.approx(6.48, abs=1e-4)   # only pm:1 counted
    assert out["paper"]["marked"] == 1 and out["paper"]["total"] == 2    # coverage exposed


@pytest.mark.asyncio
async def test_unrealized_never_raises_on_price_fn_error():
    positions = [_P("pm:1", "NO", 0.165, 48, "paper")]

    async def price_fn(mid):
        raise RuntimeError("network down")

    out = await compute_unrealized_by_mode(positions, price_fn)
    # error swallowed: position counted in total but not marked
    assert out["paper"]["unrealized"] == pytest.approx(0.0)
    assert out["paper"]["marked"] == 0 and out["paper"]["total"] == 1


@pytest.mark.asyncio
async def test_unrealized_empty():
    async def price_fn(mid):
        return 0.5
    assert await compute_unrealized_by_mode([], price_fn) == {}
