"""Regression: KalshiClient.get_market must NOT cache un-resolved (open) markets.

Bug: _markets_cache had no TTL, so an OPEN market cached during scanning
(result=None) was served forever — the directional tracker's resolution check
then never saw the market settle, and paper positions stayed open with $0 P&L.
Fix: only cache markets that have a result (finalized markets are immutable).
"""
import pytest
from unittest.mock import AsyncMock

from kalshi_client.api import KalshiClient


@pytest.mark.asyncio
async def test_open_market_is_not_cached():
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(
        return_value={"market": {"ticker": "KXOPEN", "status": "active", "yes_price": 8}}
    )
    m1 = await c.get_market("KXOPEN")
    m2 = await c.get_market("KXOPEN")
    assert m1 is not None and m1.result is None
    # Re-fetched both times — an open market must never be served from a stale cache.
    assert c._get.call_count == 2
    assert "KXOPEN" not in c._markets_cache


@pytest.mark.asyncio
async def test_resolved_market_is_cached():
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(
        return_value={
            "market": {"ticker": "KXRES", "status": "finalized", "result": "no", "yes_price": 1}
        }
    )
    m1 = await c.get_market("KXRES")
    m2 = await c.get_market("KXRES")
    assert m1 is not None and m1.result == "no"
    # Resolved markets are immutable → cached after the first fetch.
    assert c._get.call_count == 1
    assert "KXRES" in c._markets_cache


@pytest.mark.asyncio
async def test_open_then_resolved_settles_via_fresh_fetch():
    """An open market that later resolves is seen as resolved (not stale-cached)."""
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(
        return_value={"market": {"ticker": "KXW", "status": "active", "yes_price": 8}}
    )
    first = await c.get_market("KXW")  # open
    assert first.result is None
    # Market resolves; next fetch returns finalized data.
    c._get.return_value = {
        "market": {"ticker": "KXW", "status": "finalized", "result": "no", "yes_price": 1}
    }
    second = await c.get_market("KXW")  # must re-fetch, not serve stale open copy
    assert second.result == "no"
