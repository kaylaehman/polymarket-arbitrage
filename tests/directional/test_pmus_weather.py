"""Tests for PM.US tc-temp-* weather market extension.

Covers:
- parse_pmus_slug: gte_lt bucket → correct city/date/lo/hi
- parse_pmus_slug: lt bucket → correct lo/hi
- parse_pmus_slug: gte bucket (sentinel hi=999)
- parse_pmus_slug: junk input → None
- parse_pmus_slug: unknown city → None
- pmus_bucket_gate_keep: gte-only (hi=999) KEEP/SKIP
- pmus_bucket_gate_keep: regular bucket KEEP/SKIP
- PMUSWeatherSource.fetch(): mocked HTTP → KalshiMarket-compatible objects
- PM.US market through MakerLongshotStrategy.scan() → gate applied (KEEP far from bucket)
- PM.US market through scan() → gate applied (SKIP near bucket)
- Non-pmus, non-weather market → passes through unchanged
- PM.US fetch failure → degrades gracefully
"""
from __future__ import annotations

import pytest
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.weather import (
    PMUSWeatherBucket,
    PMUS_CITY_SERIES,
    parse_pmus_slug,
    pmus_bucket_gate_keep,
)
from core.directional.pmus_weather_source import PMUSWeatherSource
from core.directional.strategies.maker_longshot import MakerLongshotStrategy


# ── parse_pmus_slug ───────────────────────────────────────────────────────────

def test_parse_pmus_slug_gte_lt():
    """tc-temp-nychigh-2026-07-01-gte80lt85f → lo=80, hi=84, series=pmus:nyc."""
    wb = parse_pmus_slug("tc-temp-nychigh-2026-07-01-gte80lt85f")
    assert wb is not None
    assert wb.series == "pmus:nyc"
    assert wb.slug == "tc-temp-nychigh-2026-07-01-gte80lt85f"
    assert wb.date == date(2026, 7, 1)
    assert wb.lo == 80
    assert wb.hi == 84  # 85 - 1


def test_parse_pmus_slug_lt():
    """tc-temp-mdwhigh-2026-07-01-lt75f → lo=0, hi=74."""
    wb = parse_pmus_slug("tc-temp-mdwhigh-2026-07-01-lt75f")
    assert wb is not None
    assert wb.series == "pmus:mdw"
    assert wb.date == date(2026, 7, 1)
    assert wb.lo == 0
    assert wb.hi == 74  # 75 - 1


def test_parse_pmus_slug_gte_sentinel():
    """tc-temp-laxhigh-2026-07-01-gte90f → lo=90, hi=999 (sentinel)."""
    wb = parse_pmus_slug("tc-temp-laxhigh-2026-07-01-gte90f")
    assert wb is not None
    assert wb.series == "pmus:lax"
    assert wb.date == date(2026, 7, 1)
    assert wb.lo == 90
    assert wb.hi == 999


def test_parse_pmus_slug_mia():
    """Miami city code mdw → series pmus:mia."""
    wb = parse_pmus_slug("tc-temp-miahigh-2026-08-15-gte85lt90f")
    assert wb is not None
    assert wb.series == "pmus:mia"
    assert wb.lo == 85
    assert wb.hi == 89


def test_parse_pmus_slug_sfo():
    """San Francisco city code sfo."""
    wb = parse_pmus_slug("tc-temp-sfohigh-2026-07-20-gte65lt70f")
    assert wb is not None
    assert wb.series == "pmus:sfo"
    assert wb.lo == 65
    assert wb.hi == 69


def test_parse_pmus_slug_junk_returns_none():
    """Random junk input returns None."""
    assert parse_pmus_slug("KXHIGHNY-26JUN23-T85") is None
    assert parse_pmus_slug("") is None
    assert parse_pmus_slug("tc-temp-GARBAGE-bad") is None
    assert parse_pmus_slug("not-a-slug") is None


def test_parse_pmus_slug_unknown_city_returns_none():
    """Valid format but city not in PMUS_CITY_SERIES → None."""
    wb = parse_pmus_slug("tc-temp-xyzxyzxyz-high-2026-07-01-gte80lt85f")
    # This won't match the regex format; try a format that matches but unknown city
    # The regex requires nycHIGH format: tc-temp-{city}high-yyyy-mm-dd-{bucket}
    wb2 = parse_pmus_slug("tc-temp-phlhigh-2026-07-01-gte80lt85f")
    assert wb2 is None  # phl not in PMUS_CITY_SERIES


# ── pmus_bucket_gate_keep ─────────────────────────────────────────────────────

def make_pmus_bucket(lo: int, hi: int, city: str = "nyc") -> PMUSWeatherBucket:
    return PMUSWeatherBucket(
        series=f"pmus:{city}",
        slug=f"tc-temp-{city}high-2026-07-01-gte{lo}lt{hi+1}f",
        date=date(2026, 7, 1),
        lo=lo,
        hi=hi,
    )


def make_pmus_bucket_gte(lo: int, city: str = "nyc") -> PMUSWeatherBucket:
    return PMUSWeatherBucket(
        series=f"pmus:{city}",
        slug=f"tc-temp-{city}high-2026-07-01-gte{lo}f",
        date=date(2026, 7, 1),
        lo=lo,
        hi=999,
    )


def test_pmus_gate_keep_gte_sentinel_far_below():
    """hi=999 (gte-only): forecast well below lo - safe_margin → KEEP."""
    wb = make_pmus_bucket_gte(lo=90)
    # fc=80, lo=90, safe=4 => 80 <= 86 => KEEP
    assert pmus_bucket_gate_keep(80.0, wb, 4.0) is True


def test_pmus_gate_keep_gte_sentinel_near_threshold():
    """hi=999 (gte-only): forecast near lo → SKIP."""
    wb = make_pmus_bucket_gte(lo=90)
    # fc=87, lo=90, safe=4 => 87 <= 86 is False => SKIP
    assert pmus_bucket_gate_keep(87.0, wb, 4.0) is False


def test_pmus_gate_keep_regular_bucket_far_below():
    """Regular bucket: forecast well below lo - safe_margin → KEEP."""
    wb = make_pmus_bucket(lo=80, hi=84)
    # fc=70, lo=80, safe=4 => 70 <= 76 => KEEP
    assert pmus_bucket_gate_keep(70.0, wb, 4.0) is True


def test_pmus_gate_keep_regular_bucket_far_above():
    """Regular bucket: forecast well above hi + safe_margin → KEEP."""
    wb = make_pmus_bucket(lo=80, hi=84)
    # fc=95, hi=84, safe=4 => 95 >= 88 => KEEP
    assert pmus_bucket_gate_keep(95.0, wb, 4.0) is True


def test_pmus_gate_keep_regular_bucket_inside():
    """Regular bucket: forecast inside bucket → SKIP."""
    wb = make_pmus_bucket(lo=80, hi=84)
    # fc=82 is inside [80, 84] → SKIP
    assert pmus_bucket_gate_keep(82.0, wb, 4.0) is False


def test_pmus_gate_keep_regular_bucket_near_lo():
    """Regular bucket: forecast near lo (within safe_margin) → SKIP."""
    wb = make_pmus_bucket(lo=80, hi=84)
    # fc=78, lo=80, safe=4 => 78 <= 76 is False => SKIP
    assert pmus_bucket_gate_keep(78.0, wb, 4.0) is False


# ── PMUSWeatherSource.fetch() ─────────────────────────────────────────────────

def make_mock_http(markets_json: list) -> AsyncMock:
    """Build a mock httpx-compatible client returning given markets list."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"markets": markets_json})
    http = AsyncMock()
    http.get = AsyncMock(return_value=resp)
    return http


def make_raw_pmus_market(
    slug: str = "tc-temp-nychigh-2026-07-05-gte80lt85f",
    question: str = "Will NYC high be 80-84°F on July 5?",
    end_date: str = "2026-07-05T23:59:00Z",
    price: float = 0.10,
) -> dict:
    return {
        "slug": slug,
        "question": question,
        "endDate": end_date,
        "marketSides": [{"price": price, "tradable": True}],
    }


@pytest.mark.asyncio
async def test_pmus_source_fetch_produces_market_objects():
    """fetch() with a valid climate market returns KalshiMarket-compatible objects."""
    now_utc = datetime.now(timezone.utc)
    future_date = (now_utc + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    slug = f"tc-temp-nychigh-{(now_utc + timedelta(days=10)).strftime('%Y-%m-%d')}-gte80lt85f"
    raw = [make_raw_pmus_market(slug=slug, end_date=future_date, price=0.10)]

    http = make_mock_http(raw)
    source = PMUSWeatherSource(http=http, max_days=30.0, cache_ttl_seconds=300.0)
    markets = await source.fetch()

    assert len(markets) == 1
    mkt = markets[0]
    assert mkt.ticker == f"pmus:{slug}"
    assert mkt.event_ticker == f"pmus:{slug}"
    assert mkt.category == "weather"
    assert mkt.status == "open"
    assert mkt.yes_price == 0.10
    assert hasattr(mkt, "_no_ask")
    assert mkt._no_ask == 0.90  # 1.0 - 0.10
    assert callable(mkt.to_unified_market_id)
    assert mkt.to_unified_market_id() == f"pmus:{slug}"


@pytest.mark.asyncio
async def test_pmus_source_no_ask_method():
    """no_ask() returns the pre-fetched _no_ask for a pmus: ticker."""
    now_utc = datetime.now(timezone.utc)
    future_date = (now_utc + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    slug = f"tc-temp-nychigh-{(now_utc + timedelta(days=10)).strftime('%Y-%m-%d')}-gte80lt85f"
    raw = [make_raw_pmus_market(slug=slug, end_date=future_date, price=0.12)]

    http = make_mock_http(raw)
    source = PMUSWeatherSource(http=http, max_days=30.0)
    await source.fetch()

    no_ask = source.no_ask(f"pmus:{slug}")
    assert no_ask is not None
    assert abs(no_ask - 0.88) < 0.001  # 1.0 - 0.12


@pytest.mark.asyncio
async def test_pmus_source_fetch_failure_degrades_gracefully():
    """fetch() when HTTP fails returns empty list (no exception propagated)."""
    http = AsyncMock()
    http.get = AsyncMock(side_effect=Exception("connection refused"))
    source = PMUSWeatherSource(http=http, max_days=30.0)
    markets = await source.fetch()
    assert markets == []


# ── MakerLongshotStrategy with PM.US markets ─────────────────────────────────

def make_pmus_market(
    slug: str,
    yes_price: float = 0.10,
    no_ask: float = 0.92,
    close_days: int = 5,
) -> SimpleNamespace:
    m = SimpleNamespace()
    m.ticker = f"pmus:{slug}"
    m.event_ticker = f"pmus:{slug}"
    m.yes_price = yes_price
    m.no_price = round(1 - yes_price, 4)
    m.category = "weather"
    m.title = f"PM.US weather: {slug}"
    m.status = "open"
    m.result = None
    m.close_time = datetime.now(timezone.utc) + timedelta(days=close_days)
    m._no_ask = no_ask
    m.to_unified_market_id = lambda: f"pmus:{slug}"
    return m


def make_weather_strategy(safe_margin_f: float = 4.0, require_forecast: bool = True):
    weather_cfg = SimpleNamespace(
        enabled=True,
        safe_margin_f=safe_margin_f,
        forecast_horizon_days=7,
        require_forecast=require_forecast,
    )
    return MakerLongshotStrategy(
        min_structural_score=0.01,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,
        weather_cfg=weather_cfg,
    )


@pytest.mark.asyncio
async def test_pmus_market_gate_keep_when_forecast_far_from_bucket():
    """PM.US market: forecast far below bucket lo → weather gate KEEP → candidate emitted."""
    slug = f"tc-temp-nychigh-{(datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%d')}-gte80lt85f"
    market = make_pmus_market(slug=slug, yes_price=0.10, no_ask=0.92)
    strategy = make_weather_strategy(safe_margin_f=4.0)

    # Forecast = 70°F (well below lo=80, margin=10 → KEEP)
    mock_http = AsyncMock()
    with patch("core.directional.strategies.maker_longshot.forecast_high", new=AsyncMock(return_value=70.0)):
        ctx = {"no_ask": lambda ticker: 0.92, "http": mock_http}
        candidates = await strategy.scan([market], ctx)

    assert len(candidates) == 1
    assert candidates[0].market_id == f"pmus:{slug}"


@pytest.mark.asyncio
async def test_pmus_market_gate_skip_when_forecast_near_bucket():
    """PM.US market: forecast near lo → weather gate SKIP → no candidate."""
    slug = f"tc-temp-nychigh-{(datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%d')}-gte80lt85f"
    market = make_pmus_market(slug=slug, yes_price=0.10, no_ask=0.92)
    strategy = make_weather_strategy(safe_margin_f=4.0)

    # Forecast = 78°F (near lo=80, within safe_margin → SKIP)
    mock_http = AsyncMock()
    with patch("core.directional.strategies.maker_longshot.forecast_high", new=AsyncMock(return_value=78.0)):
        ctx = {"no_ask": lambda ticker: 0.92, "http": mock_http}
        candidates = await strategy.scan([market], ctx)

    assert len(candidates) == 0


@pytest.mark.asyncio
async def test_non_weather_market_passes_through_unchanged():
    """A non-weather, non-pmus market passes through the gate unchanged."""
    m = SimpleNamespace()
    m.ticker = "KX-POLITICS-123"
    m.event_ticker = "KX-POLITICS-123"
    m.yes_price = 0.10
    m.no_price = 0.90
    m.category = "Politics"
    m.title = "Non-weather market"
    m.status = "open"
    m.result = None
    m.close_time = datetime.now(timezone.utc) + timedelta(days=30)
    m.to_unified_market_id = lambda: "kalshi:KX-POLITICS-123"

    weather_cfg = SimpleNamespace(
        enabled=True,
        safe_margin_f=4.0,
        forecast_horizon_days=7,
        require_forecast=True,
    )
    strategy = MakerLongshotStrategy(
        min_structural_score=0.01,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,
        weather_cfg=weather_cfg,
    )

    ctx = {"no_ask": lambda ticker: 0.92, "http": AsyncMock()}
    candidates = await strategy.scan([m], ctx)
    assert len(candidates) == 1  # passes through, no gate applied


@pytest.mark.asyncio
async def test_pmus_fetch_failure_maker_still_works_with_kalshi():
    """When PM.US fetch fails, maker still processes Kalshi markets."""
    # This test verifies the engine degradation path, not strategy directly.
    # We simulate the engine's merge logic inline:
    kalshi_market = SimpleNamespace()
    kalshi_market.ticker = "KX-POLITICS-999"
    kalshi_market.event_ticker = "KX-POLITICS-999"
    kalshi_market.yes_price = 0.10
    kalshi_market.no_price = 0.90
    kalshi_market.category = "Politics"
    kalshi_market.title = "Kalshi-only market"
    kalshi_market.status = "open"
    kalshi_market.result = None
    kalshi_market.close_time = datetime.now(timezone.utc) + timedelta(days=30)
    kalshi_market.to_unified_market_id = lambda: "kalshi:KX-POLITICS-999"

    http = AsyncMock()
    http.get = AsyncMock(side_effect=Exception("PM.US offline"))
    source = PMUSWeatherSource(http=http, max_days=30.0)

    # fetch() should return [] on failure
    pmus_markets = await source.fetch()
    assert pmus_markets == []

    # Maker can still scan Kalshi markets
    strategy = MakerLongshotStrategy(
        min_structural_score=0.01,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,
    )
    ctx = {"no_ask": lambda ticker: 0.92}
    candidates = await strategy.scan([kalshi_market], ctx)
    # Should still produce a candidate for the Kalshi market
    assert len(candidates) == 1
    assert "KX-POLITICS-999" in candidates[0].market_id
