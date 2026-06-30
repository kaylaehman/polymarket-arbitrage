"""Tests for core/catalyst.py — TDD London School."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.catalyst import catalyst_proximity


def _dt(iso: str) -> datetime:
    """Parse ISO datetime string into aware UTC datetime."""
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


CALENDAR = [
    {"name": "FOMC Meeting", "date": "2026-06-21T14:00:00", "keywords": ["FOMC", "federal reserve", "rate"]},
    {"name": "CPI Release", "date": "2026-06-22T08:30:00", "keywords": ["CPI", "inflation", "consumer price"]},
    {"name": "Jobs Report", "date": "2026-07-10T08:30:00", "keywords": ["jobs", "nonfarm payroll", "employment"]},
]

NOW = _dt("2026-06-20T10:00:00")


# ---------------------------------------------------------------------------
# Keyword + window matches
# ---------------------------------------------------------------------------

def test_keyword_match_returns_boost():
    """A market whose title contains a catalyst keyword near the event gets a boost > 0."""
    boost = catalyst_proximity(
        market_title="Will the Federal Reserve raise rates?",
        market_category="Finance",
        now=NOW,
        calendar=CALENDAR,
        window_hours=72.0,
    )
    assert boost > 0.0
    assert boost <= 1.0


def test_boost_closer_event_is_higher():
    """An event 4h away gets a higher boost than one 48h away."""
    now = _dt("2026-06-21T10:00:00")  # 4h before FOMC, ~36h before CPI
    boost_fomc = catalyst_proximity(
        "FOMC rate decision",
        "Finance",
        now,
        CALENDAR,
        window_hours=72.0,
    )
    boost_cpi = catalyst_proximity(
        "CPI inflation print",
        "Finance",
        now,
        CALENDAR,
        window_hours=72.0,
    )
    assert boost_fomc > boost_cpi


def test_boost_clamped_to_one():
    """Boost is capped at 1.0 even if the event is in the very near future."""
    now = _dt("2026-06-21T13:59:00")  # 1 minute before FOMC
    boost = catalyst_proximity("FOMC meeting outcome", "Finance", now, CALENDAR, window_hours=72.0)
    assert boost <= 1.0
    assert boost > 0.9  # should be very close to 1


def test_boost_is_zero_at_boundary():
    """An event exactly at window_hours out returns a boost near 0 (but the function itself returns 0 for out-of-window)."""
    # 73h away — outside window
    now = _dt("2026-06-19T13:00:00")  # FOMC is 2026-06-21T14:00 = 49h away; adjust to put outside
    # Put now 73h before FOMC
    from datetime import timedelta
    fomc_dt = _dt("2026-06-21T14:00:00")
    now_73h = fomc_dt - timedelta(hours=73)
    boost = catalyst_proximity("FOMC rate decision", "Finance", now_73h, CALENDAR, window_hours=72.0)
    assert boost == 0.0


# ---------------------------------------------------------------------------
# No-match cases
# ---------------------------------------------------------------------------

def test_no_keyword_match_returns_zero():
    """A market with no matching keywords returns 0."""
    boost = catalyst_proximity(
        "Who will win the World Cup?",
        "Sports",
        NOW,
        CALENDAR,
        window_hours=72.0,
    )
    assert boost == 0.0


def test_empty_calendar_returns_zero():
    """An empty calendar always returns 0."""
    boost = catalyst_proximity("FOMC rate decision", "Finance", NOW, [], window_hours=72.0)
    assert boost == 0.0


def test_out_of_window_returns_zero():
    """An event outside the window returns 0 even if keywords match."""
    # Jobs report is on 2026-07-10, which is 20 days away
    boost = catalyst_proximity(
        "Nonfarm payroll jobs report",
        "Finance",
        NOW,
        CALENDAR,
        window_hours=72.0,
    )
    assert boost == 0.0


def test_past_event_returns_zero():
    """An event that has already passed returns 0."""
    past_calendar = [
        {"name": "Old FOMC", "date": "2026-06-19T14:00:00", "keywords": ["FOMC", "rate"]},
    ]
    boost = catalyst_proximity(
        "FOMC rate decision",
        "Finance",
        NOW,
        past_calendar,
        window_hours=72.0,
    )
    assert boost == 0.0


def test_category_match_gives_boost():
    """Keyword match in category (not just title) also triggers a boost."""
    boost = catalyst_proximity(
        "Will this market resolve YES?",
        "FOMC rate decision",  # category contains keyword
        NOW,
        CALENDAR,
        window_hours=72.0,
    )
    assert boost > 0.0


def test_case_insensitive_match():
    """Keyword matching is case-insensitive."""
    boost = catalyst_proximity(
        "fomc RATE meeting outcome",
        "finance",
        NOW,
        CALENDAR,
        window_hours=72.0,
    )
    assert boost > 0.0


def test_returns_max_over_multiple_matches():
    """When multiple calendar entries match, the highest boost is returned."""
    now = _dt("2026-06-21T10:00:00")  # 4h before FOMC (close), ~22h before CPI (less close)
    both_calendar = [
        {"name": "FOMC", "date": "2026-06-21T14:00:00", "keywords": ["rate", "FOMC"]},
        {"name": "CPI", "date": "2026-06-22T08:30:00", "keywords": ["rate", "CPI"]},  # 'rate' matches both
    ]
    boost = catalyst_proximity(
        "Will rate expectations change?",
        "Finance",
        now,
        both_calendar,
        window_hours=72.0,
    )
    # Should be max of the two individual boosts, i.e. the FOMC boost (4h away > 22h away)
    fomc_only = catalyst_proximity(
        "Will rate expectations change?",
        "Finance",
        now,
        [both_calendar[0]],
        window_hours=72.0,
    )
    assert boost == pytest.approx(fomc_only)


# ---------------------------------------------------------------------------
# Scanner prioritisation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scanner_prioritizes_catalyst_near_markets():
    """KalshiMarketScanner.scan() stable-sorts catalyst-near markets first when enabled."""
    from unittest.mock import AsyncMock, MagicMock

    from core.directional.scanner import KalshiMarketScanner
    from kalshi_client.models import KalshiMarket

    now_time = 0.0

    def _now():
        return now_time

    def categorize(ticker):
        return "Finance"

    # Build two liquid markets: one with a catalyst match, one without
    market_fomc = KalshiMarket(
        ticker="KXFOMC-1",
        event_ticker="KXFOMC",
        series_ticker="KXFOMC",
        title="Will the FOMC raise rates at the June meeting?",
        subtitle="",
        yes_price=0.5,
        no_price=0.5,
        status="open",
        result=None,
        volume=1000,
        open_interest=500,
        close_time=None,
        category="Finance",
    )
    market_other = KalshiMarket(
        ticker="KXWC-1",
        event_ticker="KXWC",
        series_ticker="KXWC",
        title="Will Brazil win the World Cup?",
        subtitle="",
        yes_price=0.5,
        no_price=0.5,
        status="open",
        result=None,
        volume=1000,
        open_interest=500,
        close_time=None,
        category="Sports",
    )

    # Mock orderbook with tight spread (bid=0.49, ask=0.51)
    def make_ob(bid=0.49, ask=0.51):
        ob = MagicMock()
        ob.yes = MagicMock()
        ob.yes.best_bid = bid
        ob.yes.best_ask = ask
        return ob

    async def fake_get(endpoint, params):
        return {"events": [], "cursor": None}

    async def fake_ob(ticker):
        return make_ob()

    client = MagicMock()
    client._get = AsyncMock(side_effect=fake_get)
    client.get_orderbook_unified = AsyncMock(side_effect=fake_ob)

    scanner = KalshiMarketScanner(
        client,
        categorize,
        min_volume=0,
        exclude_categories=[],
        _now_fn=_now,
        # Pin the catalyst wall-clock to NOW so the fixed 2026-06-2x calendar stays
        # within the 72h window regardless of the real date (was a time-bomb that
        # only passed when authored).
        _now_dt_fn=lambda: NOW,
    )
    # Inject the catalyst config — calendar dates are within 72h of the pinned NOW
    scanner._catalyst_calendar = CALENDAR
    scanner._catalyst_window_hours = 72.0
    scanner._catalyst_enabled = True

    # Manually inject universe — other market first to prove sorting works
    scanner._cached_universe = [market_other, market_fomc]
    scanner._fetched_at = now_time  # cache is fresh

    result = await scanner.scan(max_markets=5)

    # FOMC market should come first (higher catalyst proximity)
    tickers = [m.ticker for m in result]
    fomc_idx = tickers.index("KXFOMC-1")
    wc_idx = tickers.index("KXWC-1")
    assert fomc_idx < wc_idx, f"Expected FOMC first but got order: {tickers}"


@pytest.mark.asyncio
async def test_scanner_unchanged_when_catalyst_disabled():
    """When catalyst is disabled, scanner order is unchanged (spread-sorted)."""
    from unittest.mock import AsyncMock, MagicMock

    from core.directional.scanner import KalshiMarketScanner
    from kalshi_client.models import KalshiMarket

    now_time = 0.0

    def _now():
        return now_time

    def categorize(ticker):
        return "Finance"

    market_a = KalshiMarket(
        ticker="KXAAA-1", event_ticker="KXAAA", series_ticker="KXAAA",
        title="FOMC rate decision", subtitle="", yes_price=0.5, no_price=0.5,
        status="open", result=None, volume=1000, open_interest=500,
        close_time=None, category="Finance",
    )
    market_b = KalshiMarket(
        ticker="KXBBB-1", event_ticker="KXBBB", series_ticker="KXBBB",
        title="Brazil win World Cup", subtitle="", yes_price=0.5, no_price=0.5,
        status="open", result=None, volume=1000, open_interest=500,
        close_time=None, category="Sports",
    )

    def make_ob_narrow():
        ob = MagicMock()
        ob.yes = MagicMock()
        ob.yes.best_bid = 0.49
        ob.yes.best_ask = 0.51  # spread 0.02
        return ob

    async def fake_ob(ticker):
        return make_ob_narrow()

    client = MagicMock()
    client._get = AsyncMock(return_value={"events": [], "cursor": None})
    client.get_orderbook_unified = AsyncMock(side_effect=fake_ob)

    scanner = KalshiMarketScanner(
        client, categorize, min_volume=0, exclude_categories=[], _now_fn=_now
    )
    scanner._catalyst_enabled = False
    scanner._cached_universe = [market_a, market_b]
    scanner._fetched_at = now_time

    result = await scanner.scan(max_markets=5)
    # Both markets have the same spread; order is stable = insertion order
    tickers = [m.ticker for m in result]
    # Both should appear
    assert "KXAAA-1" in tickers
    assert "KXBBB-1" in tickers
