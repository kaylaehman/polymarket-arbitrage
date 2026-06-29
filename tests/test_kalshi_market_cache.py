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
async def test_list_markets_active_does_not_block_resolution():
    """PROD STALL BUG (2026-06-29): list_markets() cached every market
    unconditionally — incl. OPEN ones the scanner enumerates each cycle — so
    get_market() later served the stale active version and finalized markets
    never settled (positions stuck open for days, realized P&L frozen).
    list_markets must only cache RESOLVED markets."""
    c = KalshiClient(dry_run=True)
    c._get = AsyncMock(return_value={"markets": [
        {"ticker": "KXW", "status": "active", "yes_price": 8}
    ]})
    mkts, _ = await c.list_markets(series_ticker="KXW")
    assert mkts and mkts[0].result is None
    assert "KXW" not in c._markets_cache, "open market must NOT be cached by list_markets"

    # Market later finalizes — get_market must refetch and see the result.
    c._get = AsyncMock(return_value={"market": {
        "ticker": "KXW", "status": "finalized", "result": "no", "yes_price": 1}})
    m = await c.get_market("KXW")
    assert m is not None and m.result == "no"


@pytest.mark.asyncio
async def test_get_market_ignores_stale_active_cache_entry():
    """Defense in depth: even if a stale OPEN entry sits in the cache, get_market
    must refetch rather than trust an un-resolved cached market."""
    c = KalshiClient(dry_run=True)
    stale = c._parse_market({"ticker": "KXW", "status": "active", "yes_price": 8})
    assert stale is not None and stale.result is None
    c._markets_cache["KXW"] = stale  # simulate a stale active entry
    c._get = AsyncMock(return_value={"market": {
        "ticker": "KXW", "status": "finalized", "result": "yes", "yes_price": 99}})
    m = await c.get_market("KXW")
    assert m is not None and m.result == "yes"
    assert c._get.call_count == 1, "must refetch through a stale active cache entry"


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
