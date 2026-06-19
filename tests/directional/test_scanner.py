# tests/directional/test_scanner.py
"""Tests for KalshiMarketScanner (Task 8).

Mocks client.list_all_markets — no live API calls.
"""
import pytest
from core.directional.scanner import KalshiMarketScanner, is_tradeable
from kalshi_client.models import KalshiMarket


def mk(ticker, yes=0.4, vol=1000, event_ticker=None):
    """Build a minimal KalshiMarket for testing."""
    et = event_ticker or ticker.split("-")[0]
    return KalshiMarket(
        ticker=ticker,
        event_ticker=et,
        series_ticker=et,
        title=ticker,
        yes_price=yes,
        no_price=round(1.0 - yes, 4),
        volume=vol,
    )


# ---------------------------------------------------------------------------
# is_tradeable unit tests
# ---------------------------------------------------------------------------

def test_is_tradeable_normal_market():
    assert is_tradeable(mk("KX-1", yes=0.4)) is True


def test_is_tradeable_missing_yes_price():
    m = mk("KX-1", yes=0.0)  # yes_price == 0 → not tradeable
    assert is_tradeable(m) is False


def test_is_tradeable_both_prices_near_one():
    """Both prices near 1.0 is a collection/settlement artifact."""
    m = mk("KX-1", yes=0.99)
    m.no_price = 0.99
    assert is_tradeable(m) is False


def test_is_tradeable_no_price_zero():
    m = mk("KX-1", yes=0.5)
    m.no_price = 0.0
    assert is_tradeable(m) is False


# ---------------------------------------------------------------------------
# Scanner integration tests (async, mocked client)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_filters_low_volume():
    """Only markets meeting the volume floor are returned."""
    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return [
                mk("KXNFLGAME-1", vol=5),
                mk("KXNFLGAME-2", vol=5000),
            ]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Sports",
        min_volume=100,
        exclude_categories=[],
    )
    out = await sc.scan(max_markets=50)
    assert [m.ticker for m in out] == ["KXNFLGAME-2"]


@pytest.mark.asyncio
async def test_scan_excludes_category():
    """Markets whose category is in exclude_categories are dropped."""
    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return [
                mk("KXMLB-1", vol=2000),
                mk("KXETH-1", vol=3000),
            ]

    def categorize(event_ticker):
        return "Sports" if "MLB" in event_ticker else "Crypto"

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=categorize,
        min_volume=100,
        exclude_categories=["Crypto"],
    )
    out = await sc.scan(max_markets=50)
    assert len(out) == 1
    assert out[0].ticker == "KXMLB-1"


@pytest.mark.asyncio
async def test_scan_tags_category_on_market():
    """Scanner sets market.category via categorize_fn."""
    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return [mk("KXNFLGAME-1", vol=2000)]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Sports",
        min_volume=100,
        exclude_categories=[],
    )
    out = await sc.scan(max_markets=50)
    assert out[0].category == "Sports"


@pytest.mark.asyncio
async def test_scan_filters_untradeable_markets():
    """Markets where is_tradeable returns False are removed."""
    class MockClient:
        async def list_all_markets(self, status, max_markets):
            bad = mk("KXNFLGAME-BAD", yes=0.0, vol=9999)
            good = mk("KXNFLGAME-GOOD", yes=0.4, vol=9999)
            return [bad, good]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Sports",
        min_volume=100,
        exclude_categories=[],
    )
    out = await sc.scan(max_markets=50)
    assert len(out) == 1
    assert out[0].ticker == "KXNFLGAME-GOOD"


@pytest.mark.asyncio
async def test_scan_passes_max_markets_to_client():
    """max_markets argument is forwarded to list_all_markets."""
    received = {}

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            received["max_markets"] = max_markets
            return []

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Other",
        min_volume=0,
        exclude_categories=[],
    )
    await sc.scan(max_markets=123)
    assert received["max_markets"] == 123
