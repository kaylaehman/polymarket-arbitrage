"""Tests for core/weather.py and the weather gate in MakerLongshotStrategy.

All NWS HTTP calls are mocked — no live network in tests.

Covers:
- parse_weather_ticker: confirmed real ticker examples
- parse_weather_ticker: B-type buckets return None
- parse_weather_ticker: non-weather tickers return None
- parse_weather_ticker: direction split (above vs below)
- forecast_high: finds the correct isDaytime period for target date
- forecast_high: returns None when date is beyond NWS horizon
- forecast_high: swallows HTTP errors, never raises
- forecast_high: returns None for unrecognised series
- forecast_margin: arithmetic
- WeatherCfg defaults
- Weather gate: KEEPS NO bet at margin <= -safe_margin
- Weather gate: SKIPS NO bet when forecast near/above threshold
- Weather gate: SKIPS when forecast unavailable + require_forecast=True
- Weather gate: KEEPS (structural fallback) when unavailable + require_forecast=False
- Weather gate: SKIPS (beyond horizon) when require_forecast=True
- Weather gate: KEEPS (structural fallback) when beyond horizon + require_forecast=False
- Weather gate: non-weather candidates pass through untouched
- Weather gate: B-type (bucket) candidates pass through untouched (no gate)
- Weather gate: below-threshold T-type passes through untouched (no gate on cold side)
"""
from __future__ import annotations

import pytest
from datetime import date, datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from core.weather import (
    WeatherMarket,
    parse_weather_ticker,
    forecast_high,
    forecast_margin,
    SERIES_STATION,
    _forecast_cache,
)
from core.directional.strategies.maker_longshot import MakerLongshotStrategy


# ── parse_weather_ticker ──────────────────────────────────────────────────────

def test_parse_ticker_ny_above():
    """KXHIGHNY-26JUN23-T85: NYC >85F (above-threshold, YES wins if hot)."""
    wm = parse_weather_ticker("KXHIGHNY-26JUN23-T85")
    assert wm is not None
    assert wm.series == "KXHIGHNY"
    assert wm.date == date(2026, 6, 23)
    assert wm.threshold == 85.0
    assert wm.direction == "above"
    assert wm.is_above_threshold is True


def test_parse_ticker_ny_below():
    """KXHIGHNY-26JUN23-T78: NYC <78F (below-threshold, YES wins if cold)."""
    wm = parse_weather_ticker("KXHIGHNY-26JUN23-T78")
    assert wm is not None
    assert wm.series == "KXHIGHNY"
    assert wm.date == date(2026, 6, 23)
    assert wm.threshold == 78.0
    assert wm.direction == "below"
    assert wm.is_above_threshold is False


def test_parse_ticker_chi():
    """KXHIGHCHI-26JUN23-T77: Chicago >77F."""
    wm = parse_weather_ticker("KXHIGHCHI-26JUN23-T77")
    assert wm is not None
    assert wm.series == "KXHIGHCHI"
    assert wm.date == date(2026, 6, 23)
    assert wm.threshold == 77.0
    assert wm.is_above_threshold is True  # 77 >= 73 typical


def test_parse_ticker_lax():
    """KXHIGHLAX-26JUN23-T76: LA >76F."""
    wm = parse_weather_ticker("KXHIGHLAX-26JUN23-T76")
    assert wm is not None
    assert wm.series == "KXHIGHLAX"
    assert wm.threshold == 76.0
    assert wm.is_above_threshold is True  # 76 >= 72 typical


def test_parse_ticker_mia():
    """KXHIGHMIA-26JUN23-T96: Miami >96F."""
    wm = parse_weather_ticker("KXHIGHMIA-26JUN23-T96")
    assert wm is not None
    assert wm.series == "KXHIGHMIA"
    assert wm.threshold == 96.0
    assert wm.is_above_threshold is True  # 96 >= 90 typical


def test_parse_ticker_b_bucket_returns_none():
    """B-type bucket tickers are excluded (too narrow for forecast gating)."""
    assert parse_weather_ticker("KXHIGHNY-26JUN23-B78.5") is None
    assert parse_weather_ticker("KXHIGHCHI-26JUN23-B76.5") is None


def test_parse_ticker_non_weather_returns_none():
    """Non-weather tickers return None."""
    assert parse_weather_ticker("KXCPI-26JUN-T1.2") is None
    assert parse_weather_ticker("KXNFL-25JAN15-TBD") is None
    assert parse_weather_ticker("") is None
    assert parse_weather_ticker("garbage") is None


def test_parse_ticker_case_insensitive():
    """Ticker matching is case-insensitive."""
    wm = parse_weather_ticker("kxhighny-26jun23-t85")
    assert wm is not None
    assert wm.series == "KXHIGHNY"


# ── forecast_margin ───────────────────────────────────────────────────────────

def test_forecast_margin_negative():
    """Forecast 6F below threshold -> margin = -6.0."""
    assert forecast_margin(79.0, 85.0) == pytest.approx(-6.0)


def test_forecast_margin_positive():
    """Forecast above threshold -> positive margin."""
    assert forecast_margin(87.0, 85.0) == pytest.approx(2.0)


def test_forecast_margin_at_threshold():
    """Forecast exactly at threshold -> margin = 0."""
    assert forecast_margin(85.0, 85.0) == pytest.approx(0.0)


# ── SERIES_STATION map ────────────────────────────────────────────────────────

def test_series_station_has_four_confirmed_series():
    """Exactly the four confirmed series are present."""
    assert set(SERIES_STATION.keys()) == {
        "KXHIGHNY", "KXHIGHCHI", "KXHIGHLAX", "KXHIGHMIA"
    }


def test_series_station_ny_coordinates():
    """NYC station = Central Park (lat/lon within 0.1 deg of 40.78, -73.97)."""
    s = SERIES_STATION["KXHIGHNY"]
    assert abs(s.lat - 40.7829) < 0.1
    assert abs(s.lon - (-73.9654)) < 0.1


# ── forecast_high (mocked NWS) ────────────────────────────────────────────────

def _make_nws_periods(target_date: date, temp: float) -> list[dict]:
    """Build a minimal NWS periods list with one daytime entry on target_date."""
    return [
        {
            "isDaytime": False,
            "startTime": target_date.isoformat() + "T18:00:00-04:00",
            "temperature": 65,
            "temperatureUnit": "F",
            "name": "Tonight",
        },
        {
            "isDaytime": True,
            "startTime": target_date.isoformat() + "T06:00:00-04:00",
            "temperature": int(temp),
            "temperatureUnit": "F",
            "name": "Tuesday",
        },
    ]


def _make_http_mock(periods: list | None, *, fail_points: bool = False) -> MagicMock:
    """Build an async http mock that returns the NWS response structure."""
    http = MagicMock()
    if fail_points:
        http.get = AsyncMock(side_effect=Exception("network error"))
        return http

    points_resp = MagicMock()
    points_resp.status_code = 200
    points_resp.json = MagicMock(return_value={
        "properties": {"forecast": "https://api.weather.gov/gridpoints/OKX/34,45/forecast"}
    })

    if periods is None:
        forecast_resp = MagicMock()
        forecast_resp.status_code = 500
        forecast_resp.json = MagicMock(return_value={})
        http.get = AsyncMock(side_effect=[points_resp, forecast_resp])
    else:
        forecast_resp = MagicMock()
        forecast_resp.status_code = 200
        forecast_resp.json = MagicMock(return_value={
            "properties": {"periods": periods}
        })
        http.get = AsyncMock(side_effect=[points_resp, forecast_resp])

    return http


@pytest.mark.asyncio
async def test_forecast_high_returns_correct_daytime_temp():
    """forecast_high returns the isDaytime=True period temperature."""
    target = date(2026, 6, 23)
    periods = _make_nws_periods(target, temp=75.0)
    http = _make_http_mock(periods)
    # Clear cache to avoid cross-test contamination
    _forecast_cache.clear()

    result = await forecast_high("KXHIGHNY", target, http=http)
    assert result == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_forecast_high_returns_none_when_date_not_in_periods():
    """forecast_high returns None when target date is not in the forecast (beyond horizon)."""
    near = date(2026, 6, 23)
    periods = _make_nws_periods(near, temp=75.0)  # only Jun 23 in periods
    http = _make_http_mock(periods)
    _forecast_cache.clear()

    far = date(2026, 7, 5)  # not in periods
    result = await forecast_high("KXHIGHNY", far, http=http)
    assert result is None


@pytest.mark.asyncio
async def test_forecast_high_swallows_http_error_returns_none():
    """forecast_high returns None (never raises) when NWS returns an error."""
    http = _make_http_mock(None, fail_points=True)
    _forecast_cache.clear()

    result = await forecast_high("KXHIGHNY", date(2026, 6, 23), http=http)
    assert result is None


@pytest.mark.asyncio
async def test_forecast_high_swallows_500_forecast_error():
    """forecast_high returns None when forecast endpoint returns 500."""
    http = _make_http_mock(None)  # forecast_resp is 500
    _forecast_cache.clear()

    result = await forecast_high("KXHIGHNY", date(2026, 6, 23), http=http)
    assert result is None


@pytest.mark.asyncio
async def test_forecast_high_unknown_series_returns_none():
    """forecast_high returns None for a series not in SERIES_STATION."""
    http = _make_http_mock([])
    _forecast_cache.clear()

    result = await forecast_high("KXHIGHATL", date(2026, 6, 23), http=http)
    assert result is None
    # Should not have called http.get at all (station lookup fails first)
    http.get.assert_not_called()


# ── Weather gate in MakerLongshotStrategy ────────────────────────────────────

def _weather_cfg(
    enabled=True,
    safe_margin_f=4.0,
    forecast_horizon_days=7,
    require_forecast=True,
):
    return SimpleNamespace(
        enabled=enabled,
        safe_margin_f=safe_margin_f,
        forecast_horizon_days=forecast_horizon_days,
        require_forecast=require_forecast,
    )


def _make_weather_market(
    ticker="KXHIGHNY-26JUN23-T85",
    yes_price=0.08,
    close_days=3,
):
    """Build a SimpleNamespace market matching a weather ticker."""
    m = SimpleNamespace()
    m.ticker = ticker
    m.event_ticker = ticker
    m.yes_price = yes_price
    m.no_price = 1.0 - yes_price
    m.category = "Weather"
    m.title = "Will the high temp in NYC be >85F on Jun 23, 2026?"
    m.status = "open"
    m.result = None
    m.close_time = datetime.now(timezone.utc) + timedelta(days=close_days)
    m.to_unified_market_id = lambda: f"kalshi:{ticker}"
    return m


def _make_strategy(weather_cfg=None):
    return MakerLongshotStrategy(
        min_structural_score=0.01,
        max_yes_price=0.20,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.02,
        max_days_to_resolution=9999.0,
        weather_cfg=weather_cfg,
    )


def _ctx(no_ask=0.94, http=None):
    return {"no_ask": lambda _: no_ask, "http": http}


@pytest.mark.asyncio
async def test_gate_keeps_no_bet_when_margin_below_safe():
    """NO bet KEPT: forecast=79, threshold=85, margin=-6 <= -4 (safe_margin_f=4)."""
    target = date(2026, 6, 23)
    periods = _make_nws_periods(target, temp=79.0)
    http = _make_http_mock(periods)
    _forecast_cache.clear()

    strategy = _make_strategy(weather_cfg=_weather_cfg(safe_margin_f=4.0))
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert len(candidates) == 1
    assert candidates[0].side == "NO"


@pytest.mark.asyncio
async def test_gate_skips_no_bet_when_forecast_near_threshold():
    """NO bet SKIPPED: forecast=82, threshold=85, margin=-3 > -4 (too close)."""
    target = date(2026, 6, 23)
    periods = _make_nws_periods(target, temp=82.0)
    http = _make_http_mock(periods)
    _forecast_cache.clear()

    strategy = _make_strategy(weather_cfg=_weather_cfg(safe_margin_f=4.0))
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert candidates == []


@pytest.mark.asyncio
async def test_gate_skips_when_forecast_above_threshold():
    """NO bet SKIPPED: forecast=87, threshold=85, margin=+2 > -4."""
    target = date(2026, 6, 23)
    periods = _make_nws_periods(target, temp=87.0)
    http = _make_http_mock(periods)
    _forecast_cache.clear()

    strategy = _make_strategy(weather_cfg=_weather_cfg(safe_margin_f=4.0))
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert candidates == []


@pytest.mark.asyncio
async def test_gate_skips_when_forecast_unavailable_require_forecast_true():
    """NO bet SKIPPED when forecast unavailable + require_forecast=True."""
    http = _make_http_mock(None, fail_points=True)  # NWS call fails
    _forecast_cache.clear()

    cfg = _weather_cfg(require_forecast=True)
    strategy = _make_strategy(weather_cfg=cfg)
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert candidates == []


@pytest.mark.asyncio
async def test_gate_keeps_structural_fallback_when_forecast_unavailable_require_false():
    """NO bet KEPT (structural fallback) when forecast unavailable + require_forecast=False."""
    http = _make_http_mock(None, fail_points=True)
    _forecast_cache.clear()

    cfg = _weather_cfg(require_forecast=False)
    strategy = _make_strategy(weather_cfg=cfg)
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_gate_skips_beyond_horizon_require_forecast_true():
    """NO bet SKIPPED when market closes beyond forecast_horizon_days + require_forecast=True."""
    cfg = _weather_cfg(forecast_horizon_days=7, require_forecast=True)
    strategy = _make_strategy(weather_cfg=cfg)
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=10)
    # No http needed — horizon check fires before NWS call
    candidates = await strategy.scan([m], _ctx(http=MagicMock()))
    assert candidates == []


@pytest.mark.asyncio
async def test_gate_keeps_beyond_horizon_require_forecast_false():
    """NO bet KEPT (structural fallback) when beyond horizon + require_forecast=False."""
    cfg = _weather_cfg(forecast_horizon_days=7, require_forecast=False)
    strategy = _make_strategy(weather_cfg=cfg)
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=10)
    candidates = await strategy.scan([m], _ctx(http=MagicMock()))
    assert len(candidates) == 1


@pytest.mark.asyncio
async def test_gate_non_weather_candidate_passes_through():
    """Non-weather markets (e.g. KXCPI) pass through the gate unchanged."""
    cfg = _weather_cfg()
    strategy = _make_strategy(weather_cfg=cfg)

    m = SimpleNamespace()
    m.ticker = "KXCPI-26JUL-T0.2"
    m.event_ticker = "KXCPI-26JUL"
    m.yes_price = 0.08
    m.no_price = 0.92
    m.category = "Economics"
    m.title = "Will CPI be 0.2?"
    m.status = "open"
    m.result = None
    m.close_time = datetime.now(timezone.utc) + timedelta(days=5)
    m.to_unified_market_id = lambda: "kalshi:KXCPI-26JUL-T0.2"

    # No http call expected — non-weather bypasses gate entirely
    http = MagicMock()
    http.get = AsyncMock()
    candidates = await strategy.scan([m], _ctx(http=http))
    assert len(candidates) == 1
    http.get.assert_not_called()


@pytest.mark.asyncio
async def test_gate_bucket_b_type_passes_through():
    """B-type bucket markets pass through untouched (parse returns None, no gate)."""
    cfg = _weather_cfg()
    strategy = _make_strategy(weather_cfg=cfg)

    m = SimpleNamespace()
    m.ticker = "KXHIGHNY-26JUN23-B78.5"
    m.event_ticker = "KXHIGHNY-26JUN23"
    m.yes_price = 0.08
    m.no_price = 0.92
    m.category = "Weather"
    m.title = "Will NYC high be 78-79F on Jun 23?"
    m.status = "open"
    m.result = None
    m.close_time = datetime.now(timezone.utc) + timedelta(days=3)
    m.to_unified_market_id = lambda: "kalshi:KXHIGHNY-26JUN23-B78.5"

    http = MagicMock()
    http.get = AsyncMock()
    candidates = await strategy.scan([m], _ctx(http=http))
    # B-type: parse_weather_ticker returns None -> no gate -> passes through
    assert len(candidates) == 1
    http.get.assert_not_called()


@pytest.mark.asyncio
async def test_gate_below_threshold_t_type_passes_through():
    """Below-threshold T-type (cold-side) passes through — gate only applies to above."""
    cfg = _weather_cfg()
    strategy = _make_strategy(weather_cfg=cfg)

    # T78 with NYC typical 80F -> direction="below" -> is_above_threshold=False
    m = _make_weather_market("KXHIGHNY-26JUN23-T78", yes_price=0.08, close_days=3)

    http = MagicMock()
    http.get = AsyncMock()
    candidates = await strategy.scan([m], _ctx(http=http))
    # below-threshold: not gated -> passes through
    assert len(candidates) == 1
    http.get.assert_not_called()


@pytest.mark.asyncio
async def test_gate_disabled_weather_candidate_passes_through():
    """With weather gate disabled (enabled=False), weather candidates pass through."""
    cfg = _weather_cfg(enabled=False)
    strategy = _make_strategy(weather_cfg=cfg)

    http = MagicMock()
    http.get = AsyncMock()
    m = _make_weather_market("KXHIGHNY-26JUN23-T85", yes_price=0.08, close_days=3)
    candidates = await strategy.scan([m], _ctx(http=http))
    assert len(candidates) == 1
    http.get.assert_not_called()


# ── WeatherCfg defaults ───────────────────────────────────────────────────────

def test_weather_cfg_defaults():
    """WeatherCfg dataclass defaults match spec."""
    from utils.config_loader import WeatherCfg
    cfg = WeatherCfg()
    assert cfg.enabled is True
    assert cfg.safe_margin_f == 4.0
    assert cfg.forecast_horizon_days == 7
    assert cfg.require_forecast is True


def test_directional_config_has_weather():
    """DirectionalConfig exposes weather attribute with WeatherCfg defaults."""
    from utils.config_loader import DirectionalConfig
    cfg = DirectionalConfig()
    assert hasattr(cfg, "weather")
    assert cfg.weather.enabled is True
    assert cfg.weather.safe_margin_f == 4.0
