import pytest
from unittest.mock import AsyncMock
from kalshi_client.api import KalshiClient


@pytest.mark.asyncio
async def test_orderbook_fp_dollars_format():
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(return_value={"orderbook_fp": {
        "yes_dollars": [["0.45", "5"], ["0.40", "10"]],
        "no_dollars": [["0.50", "3"]]}})
    ob = await c.get_orderbook("KX-1")
    assert ob is not None
    assert sorted(l.price for l in ob.yes_bids) == [0.40, 0.45]
    assert ob.no_bids[0].price == 0.50
    assert ob.yes_bids[0].price == 0.45  # sorted best-first


@pytest.mark.asyncio
async def test_orderbook_legacy_cents_format():
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(return_value={"orderbook": {"yes": [[45, 5]], "no": [[50, 3]]}})
    ob = await c.get_orderbook("KX-1")
    assert ob.yes_bids[0].price == 0.45
    assert ob.no_bids[0].price == 0.50


@pytest.mark.asyncio
async def test_orderbook_empty_returns_none():
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(return_value={})
    assert await c.get_orderbook("KX-1") is None
