# tests/directional/test_scanner.py
"""Tests for KalshiMarketScanner (redesigned: events-endpoint + orderbook probe).

All tests mock:
  - client._get  → returns synthetic /events API responses
  - client.get_orderbook_unified → returns synthetic OrderBook-like objects

No live API calls.
"""
import pytest
from types import SimpleNamespace
from typing import Optional

from core.directional.scanner import KalshiMarketScanner, _is_parlay, _parse_market_from_dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ob(bid: Optional[float], ask: Optional[float], no_ask: Optional[float] = None):
    """Build a minimal fake unified OrderBook."""
    yes = SimpleNamespace(best_bid=bid, best_ask=ask)
    _no_ask = no_ask if no_ask is not None else (round(1.0 - bid, 4) if bid is not None else None)
    no = SimpleNamespace(best_bid=None, best_ask=_no_ask)
    return SimpleNamespace(yes=yes, no=no)


def _market_dict(ticker, event_ticker=None, series_ticker=None, title=None):
    """Build a raw dict as returned by /events nested markets array."""
    return {
        "ticker": ticker,
        "event_ticker": event_ticker or ticker.rsplit("-", 1)[0],
        "series_ticker": series_ticker or ticker.split("-")[0],
        "title": title or f"Test market {ticker}",
        "status": "open",
        "close_time": None,
        "volume": None,       # Intentionally None — the endpoint omits this
        "yes_price": None,    # Intentionally None
        "no_price": None,
    }


def _events_response(*tickers, cursor=None):
    """Build a fake /events API response containing the given tickers."""
    markets = [_market_dict(t) for t in tickers]
    return {"events": [{"markets": markets, "event_ticker": "TEST"}], "cursor": cursor}


class MockClient:
    """Configurable fake client for scanner tests."""

    def __init__(self, events_pages=None, ob_map=None):
        """
        events_pages: list of dicts returned by successive _get calls
        ob_map: dict of ticker -> OrderBook (or None) returned by get_orderbook_unified
        """
        self._pages = list(events_pages or [_events_response()])
        self._page_idx = 0
        self._ob_map = ob_map or {}
        self.get_calls = []
        self.ob_calls = []

    async def _get(self, endpoint, params=None):
        self.get_calls.append((endpoint, params))
        if self._page_idx < len(self._pages):
            page = self._pages[self._page_idx]
            self._page_idx += 1
            return page
        return {"events": [], "cursor": None}

    async def get_orderbook_unified(self, ticker):
        self.ob_calls.append(ticker)
        return self._ob_map.get(ticker)


# ---------------------------------------------------------------------------
# _is_parlay unit tests
# ---------------------------------------------------------------------------

def test_is_parlay_kxmv_prefix():
    assert _is_parlay("KXMV-SOMETHING") is True


def test_is_parlay_multigame_substring():
    assert _is_parlay("KX-MULTIGAME-ABC") is True


def test_is_parlay_multimarket_substring():
    assert _is_parlay("KXNFL-MULTIMARKET-01") is True


def test_is_parlay_normal_ticker():
    assert _is_parlay("KXFED-25DEC-T50") is False


def test_is_parlay_case_insensitive():
    assert _is_parlay("kxmv-abc") is True


# ---------------------------------------------------------------------------
# _parse_market_from_dict unit tests
# ---------------------------------------------------------------------------

def test_parse_market_from_dict_basic():
    raw = _market_dict("KXFED-1", event_ticker="KXFED", series_ticker="KXFED")
    m = _parse_market_from_dict(raw)
    assert m is not None
    assert m.ticker == "KXFED-1"
    assert m.event_ticker == "KXFED"
    assert m.yes_price == 0.0   # not set until orderbook probe
    assert m.volume == 0        # None coerced to 0


def test_parse_market_from_dict_missing_ticker_returns_none():
    assert _parse_market_from_dict({}) is None
    assert _parse_market_from_dict({"ticker": ""}) is None


# ---------------------------------------------------------------------------
# Scanner — universe fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_fetches_events_endpoint():
    """scanner._get is called with /events and the correct params."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, 0.45)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=5)

    assert any("/events" in str(call) for call in client.get_calls), (
        f"Expected /events call, got: {client.get_calls}"
    )


@pytest.mark.asyncio
async def test_scan_excludes_kxmv_parlay():
    """KXMV tickers are excluded from the universe."""
    client = MockClient(
        events_pages=[_events_response("KXMV-PARLAY-01", "KXPOL-1")],
        ob_map={
            "KXMV-PARLAY-01": _ob(0.40, 0.45),
            "KXPOL-1": _ob(0.40, 0.45),
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)

    tickers = [m.ticker for m in result]
    assert "KXMV-PARLAY-01" not in tickers
    assert "KXPOL-1" in tickers


@pytest.mark.asyncio
async def test_scan_excludes_multigame_parlay():
    """Tickers containing MULTIGAME are excluded."""
    client = MockClient(
        events_pages=[_events_response("KX-MULTIGAME-01", "KXECO-1")],
        ob_map={
            "KX-MULTIGAME-01": _ob(0.40, 0.45),
            "KXECO-1": _ob(0.40, 0.45),
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Economics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)

    assert not any("MULTIGAME" in m.ticker for m in result)
    assert "KXECO-1" in [m.ticker for m in result]


# ---------------------------------------------------------------------------
# Scanner — liquidity filter (two-sided YES book)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_keeps_two_sided_book():
    """A market with both YES best_bid and best_ask is kept."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, 0.50)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert len(result) == 1
    assert result[0].ticker == "KXPOL-1"


@pytest.mark.asyncio
async def test_scan_drops_one_sided_book_no_bid():
    """A market with no YES best_bid is dropped."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(None, 0.50)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


@pytest.mark.asyncio
async def test_scan_drops_one_sided_book_no_ask():
    """A market with no YES best_ask is dropped."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, None)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


@pytest.mark.asyncio
async def test_scan_drops_none_orderbook():
    """A market whose orderbook returns None is dropped."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": None},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


@pytest.mark.asyncio
async def test_scan_drops_wide_spread():
    """A market with spread > MAX_SPREAD (0.20) is dropped."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.30, 0.55)},  # spread = 0.25 > 0.20
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


@pytest.mark.asyncio
async def test_scan_keeps_tight_spread_at_boundary():
    """A market with spread exactly MAX_SPREAD (0.20) is kept."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.30, 0.50)},  # spread = 0.20 exactly
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Scanner — yes_price attachment from orderbook mid
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_attaches_yes_price_from_mid():
    """market.yes_price is set to round((bid+ask)/2, 4) from the YES book."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, 0.50)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert len(result) == 1
    assert result[0].yes_price == 0.45


@pytest.mark.asyncio
async def test_scan_attaches_yes_price_rounded():
    """yes_price rounding to 4 decimal places."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.3333, 0.4444)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result[0].yes_price == round((0.3333 + 0.4444) / 2, 4)


# ---------------------------------------------------------------------------
# Scanner — category tagging and exclusion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_tags_category():
    """scanner.categorize_fn result is set on market.category."""
    client = MockClient(
        events_pages=[_events_response("KXFED-1")],
        ob_map={"KXFED-1": _ob(0.40, 0.50)},
    )
    sc = KalshiMarketScanner(client, lambda t: "Economics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result[0].category == "Economics"


@pytest.mark.asyncio
async def test_scan_excludes_categories():
    """Markets in exclude_categories are dropped even if liquid."""
    client = MockClient(
        events_pages=[_events_response("KXMLB-1", "KXFED-1")],
        ob_map={
            "KXMLB-1": _ob(0.40, 0.50),
            "KXFED-1": _ob(0.40, 0.50),
        },
    )

    def categorize(event_ticker):
        return "Sports" if "MLB" in event_ticker else "Economics"

    sc = KalshiMarketScanner(client, categorize, min_volume=0, exclude_categories=["Sports"])
    result = await sc.scan(max_markets=10)
    tickers = [m.ticker for m in result]
    assert "KXMLB-1" not in tickers
    assert "KXFED-1" in tickers


# ---------------------------------------------------------------------------
# Scanner — max_markets cap
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_caps_to_max_markets():
    """scan() returns at most max_markets even if more pass all filters."""
    tickers = [f"KXPOL-{i}" for i in range(20)]
    ob_map = {t: _ob(0.40, 0.50) for t in tickers}
    client = MockClient(
        events_pages=[_events_response(*tickers)],
        ob_map=ob_map,
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=5)
    assert len(result) == 5


@pytest.mark.asyncio
async def test_scan_returns_all_when_fewer_than_cap():
    """If fewer markets survive than max_markets, all are returned."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1", "KXPOL-2")],
        ob_map={
            "KXPOL-1": _ob(0.40, 0.50),
            "KXPOL-2": _ob(0.40, 0.50),
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=20)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# Scanner — probe count bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_bounds_orderbook_probes():
    """Orderbook probes are capped to max(60, max_markets*3) candidates."""
    tickers = [f"KXPOL-{i}" for i in range(100)]
    ob_map = {t: _ob(0.40, 0.50) for t in tickers}
    client = MockClient(
        events_pages=[_events_response(*tickers)],
        ob_map=ob_map,
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=10)
    # max(60, 10*3) = 60 probes
    assert len(client.ob_calls) <= 60


@pytest.mark.asyncio
async def test_scan_probes_at_least_60_when_available():
    """Probe limit is at least 60 regardless of max_markets."""
    tickers = [f"KXPOL-{i}" for i in range(80)]
    ob_map = {t: _ob(0.40, 0.50) for t in tickers}
    client = MockClient(
        events_pages=[_events_response(*tickers)],
        ob_map=ob_map,
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=5)
    # Even though max_markets=5, we probe up to max(60, 5*3)=60
    assert len(client.ob_calls) == 60


# ---------------------------------------------------------------------------
# Scanner — last_books and no_ask helper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_last_books_populated_after_scan():
    """scanner.last_books is keyed by ticker after scan()."""
    ob = _ob(0.40, 0.50)
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": ob},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=10)
    assert "KXPOL-1" in sc.last_books


@pytest.mark.asyncio
async def test_no_ask_returns_no_best_ask():
    """scanner.no_ask(ticker) returns the NO side best_ask from last_books."""
    ob = _ob(0.40, 0.50, no_ask=0.62)
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": ob},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=10)
    assert sc.no_ask("KXPOL-1") == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_no_ask_returns_none_for_missing_ticker():
    """scanner.no_ask returns None for a ticker not in last_books."""
    client = MockClient(events_pages=[_events_response()], ob_map={})
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=10)
    assert sc.no_ask("DOES-NOT-EXIST") is None


@pytest.mark.asyncio
async def test_last_books_not_populated_for_dropped_markets():
    """Markets dropped for missing/one-sided books are not in last_books."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1", "KXPOL-2")],
        ob_map={
            "KXPOL-1": _ob(0.40, 0.50),
            "KXPOL-2": None,   # no book → dropped
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    await sc.scan(max_markets=10)
    assert "KXPOL-1" in sc.last_books
    assert "KXPOL-2" not in sc.last_books


# ---------------------------------------------------------------------------
# Scanner — TTL cache on universe (not on orderbook probes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_universe_cache_within_ttl():
    """_get /events is called only once when two scans happen inside the TTL."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, 0.50)},
    )
    t = [0.0]
    sc = KalshiMarketScanner(
        client,
        lambda _: "Politics",
        min_volume=0,
        exclude_categories=[],
        cache_ttl_seconds=600,
        _now_fn=lambda: t[0],
    )

    await sc.scan(max_markets=5)
    t[0] = 300.0  # inside TTL
    await sc.scan(max_markets=5)

    # /events called only once (second scan uses cache)
    events_calls = [c for c in client.get_calls if "/events" in str(c)]
    assert len(events_calls) == 1, f"Expected 1 /events call, got {len(events_calls)}"


@pytest.mark.asyncio
async def test_universe_cache_refetch_after_ttl():
    """_get /events is called again once the TTL has elapsed."""
    client = MockClient(
        events_pages=[
            _events_response("KXPOL-1"),
            _events_response("KXPOL-1"),   # second page for the refetch
        ],
        ob_map={"KXPOL-1": _ob(0.40, 0.50)},
    )
    t = [0.0]
    sc = KalshiMarketScanner(
        client,
        lambda _: "Politics",
        min_volume=0,
        exclude_categories=[],
        cache_ttl_seconds=600,
        _now_fn=lambda: t[0],
    )

    await sc.scan(max_markets=5)
    t[0] = 601.0  # past TTL
    await sc.scan(max_markets=5)

    events_calls = [c for c in client.get_calls if "/events" in str(c)]
    assert len(events_calls) >= 2, f"Expected >=2 /events calls, got {len(events_calls)}"


# ---------------------------------------------------------------------------
# Scanner — sort order (tightest spread first)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_sorted_by_tightest_spread():
    """Results are sorted tightest spread first."""
    client = MockClient(
        events_pages=[_events_response("KXPOL-1", "KXPOL-2", "KXPOL-3")],
        ob_map={
            "KXPOL-1": _ob(0.30, 0.50),   # spread 0.20
            "KXPOL-2": _ob(0.42, 0.48),   # spread 0.06  <- tightest
            "KXPOL-3": _ob(0.35, 0.50),   # spread 0.15
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    tickers = [m.ticker for m in result]
    assert tickers[0] == "KXPOL-2", f"Expected tightest-spread first, got {tickers}"


# ---------------------------------------------------------------------------
# Scanner — min_volume parameter is accepted but unused
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_min_volume_param_accepted_and_unused():
    """min_volume is accepted by the constructor but does not filter liquid markets.

    The /events endpoint does not return volume; all liquid markets pass.
    """
    client = MockClient(
        events_pages=[_events_response("KXPOL-1")],
        ob_map={"KXPOL-1": _ob(0.40, 0.50)},
    )
    # Even with a very high min_volume, the market should pass (volume field = 0)
    sc = KalshiMarketScanner(
        client, lambda t: "Politics", min_volume=999999, exclude_categories=[]
    )
    result = await sc.scan(max_markets=10)
    # Accepted-but-unused: the market still appears
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Scanner — empty universe / all parlays
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_all_parlays_returns_empty():
    """If the universe contains only parlays, scan returns []."""
    client = MockClient(
        events_pages=[_events_response("KXMV-COMBO-01", "KXMV-COMBO-02")],
        ob_map={
            "KXMV-COMBO-01": _ob(0.40, 0.50),
            "KXMV-COMBO-02": _ob(0.40, 0.50),
        },
    )
    sc = KalshiMarketScanner(client, lambda t: "Sports", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


@pytest.mark.asyncio
async def test_scan_empty_universe_returns_empty():
    """An /events response with no markets returns []."""
    client = MockClient(
        events_pages=[{"events": [], "cursor": None}],
        ob_map={},
    )
    sc = KalshiMarketScanner(client, lambda t: "Politics", min_volume=0, exclude_categories=[])
    result = await sc.scan(max_markets=10)
    assert result == []


# ---------------------------------------------------------------------------
# Scanner — max_spread constructor parameter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_max_spread_excludes_wide_book_at_default():
    """A market with YES spread ~0.5 is excluded when max_spread=0.20 (default)."""
    # bid=0.25, ask=0.75 → spread=0.50 > 0.20
    client = MockClient(
        events_pages=[_events_response("KXPOL-WIDE")],
        ob_map={"KXPOL-WIDE": _ob(0.25, 0.75)},
    )
    sc = KalshiMarketScanner(
        client, lambda t: "Politics", min_volume=0, exclude_categories=[], max_spread=0.20
    )
    result = await sc.scan(max_markets=10)
    assert result == [], "Wide-spread market should be excluded at default max_spread=0.20"


@pytest.mark.asyncio
async def test_scan_max_spread_includes_wide_book_when_relaxed():
    """A market with YES spread ~0.5 is included when max_spread=0.99 (arb mode)."""
    # bid=0.25, ask=0.75 → spread=0.50 <= 0.99
    client = MockClient(
        events_pages=[_events_response("KXPOL-WIDE")],
        ob_map={"KXPOL-WIDE": _ob(0.25, 0.75)},
    )
    sc = KalshiMarketScanner(
        client, lambda t: "Politics", min_volume=0, exclude_categories=[], max_spread=0.99
    )
    result = await sc.scan(max_markets=10)
    assert len(result) == 1, "Wide-spread market should be included at max_spread=0.99"
    assert result[0].ticker == "KXPOL-WIDE"
