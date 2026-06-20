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


# ---------------------------------------------------------------------------
# Fix 1: CAP after filtering — scan() must return at most max_markets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_caps_to_max_markets():
    """After filtering, scan() returns at most max_markets, sorted by volume desc."""
    markets = [mk(f"KXFOO-{i}", vol=(i + 1) * 100) for i in range(50)]

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return markets  # returns all 50 regardless of max_markets arg

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Politics",
        min_volume=50,
        exclude_categories=[],
    )
    result = await sc.scan(max_markets=10)
    assert len(result) == 10
    # Must be the 10 highest-volume markets
    volumes = [m.volume for m in result]
    assert volumes == sorted(volumes, reverse=True), "results must be sorted by volume descending"
    assert min(volumes) > max(m.volume for m in markets) - 10 * 100 - 1


@pytest.mark.asyncio
async def test_scan_returns_all_when_fewer_than_cap():
    """If filtered result < max_markets, returns all surviving markets (no padding)."""
    markets = [mk(f"KXBAR-{i}", vol=1000) for i in range(5)]

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return markets

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda t: "Finance",
        min_volume=100,
        exclude_categories=[],
    )
    result = await sc.scan(max_markets=20)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# Fix 2: TTL cache — list_all_markets is not called on every scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_uses_cache_within_ttl():
    """list_all_markets is called only once when two scans happen inside the TTL."""
    call_count = {"n": 0}

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            call_count["n"] += 1
            return [mk("KXPOL-1", vol=500)]

    t = [0.0]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda _: "Politics",
        min_volume=100,
        exclude_categories=[],
        cache_ttl_seconds=600,
        _now_fn=lambda: t[0],
    )

    await sc.scan(max_markets=10)
    t[0] = 300.0  # still inside 600s TTL
    await sc.scan(max_markets=10)

    assert call_count["n"] == 1, (
        f"Expected list_all_markets called once (cache hit), got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_scan_refetches_after_ttl():
    """list_all_markets is called again once the TTL has elapsed."""
    call_count = {"n": 0}

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            call_count["n"] += 1
            return [mk("KXPOL-1", vol=500)]

    t = [0.0]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda _: "Politics",
        min_volume=100,
        exclude_categories=[],
        cache_ttl_seconds=600,
        _now_fn=lambda: t[0],
    )

    await sc.scan(max_markets=10)
    t[0] = 601.0  # past TTL
    await sc.scan(max_markets=10)

    assert call_count["n"] == 2, (
        f"Expected list_all_markets called twice (TTL expired), got {call_count['n']}"
    )


@pytest.mark.asyncio
async def test_scan_filter_and_cap_applied_on_cache_hit():
    """Filter + cap are applied fresh each scan even when the raw list is cached."""
    markets = [mk(f"KXECO-{i}", vol=(i + 1) * 200) for i in range(20)]

    class MockClient:
        async def list_all_markets(self, status, max_markets):
            return markets

    t = [0.0]

    sc = KalshiMarketScanner(
        MockClient(),
        categorize_fn=lambda _: "Economics",
        min_volume=100,
        exclude_categories=[],
        cache_ttl_seconds=600,
        _now_fn=lambda: t[0],
    )

    r1 = await sc.scan(max_markets=5)
    t[0] = 10.0  # cache still valid
    r2 = await sc.scan(max_markets=3)  # smaller cap on second call

    assert len(r1) == 5
    assert len(r2) == 3
