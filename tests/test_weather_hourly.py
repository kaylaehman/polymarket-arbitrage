"""Unit test for core.weather.forecast_hour — the NWS hourly forecaster added
for KXTEMP<CITY>H hourly-directional-temperature markets.

Mocks the http client (points -> forecastHourly); no live network.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.weather import forecast_hour, _forecast_cache


def _make_hourly_http_mock(periods: list) -> MagicMock:
    """Build an async http mock for points -> properties.forecastHourly."""
    http = MagicMock()

    points_resp = MagicMock()
    points_resp.status_code = 200
    points_resp.json = MagicMock(return_value={
        "properties": {
            "forecast": "https://api.weather.gov/gridpoints/OKX/34,45/forecast",
            "forecastHourly": "https://api.weather.gov/gridpoints/OKX/34,45/forecast/hourly",
        }
    })

    hourly_resp = MagicMock()
    hourly_resp.status_code = 200
    hourly_resp.json = MagicMock(return_value={"properties": {"periods": periods}})

    http.get = AsyncMock(side_effect=[points_resp, hourly_resp])
    return http


@pytest.mark.asyncio
async def test_forecast_hour_returns_temp_for_matching_hour():
    periods = [
        {"startTime": "2026-06-30T16:00:00-04:00", "temperature": 88},
        {"startTime": "2026-06-30T17:00:00-04:00", "temperature": 91},
        {"startTime": "2026-06-30T18:00:00-04:00", "temperature": 90},
    ]
    http = _make_hourly_http_mock(periods)
    _forecast_cache.clear()

    result = await forecast_hour("KXTEMPNYCH", "2026-06-30T17", http=http)
    assert result == pytest.approx(91.0)


@pytest.mark.asyncio
async def test_forecast_hour_returns_none_for_unknown_series():
    http = MagicMock()
    http.get = AsyncMock()
    _forecast_cache.clear()

    result = await forecast_hour("KXTEMPATLH", "2026-06-30T17", http=http)
    assert result is None
    http.get.assert_not_called()
