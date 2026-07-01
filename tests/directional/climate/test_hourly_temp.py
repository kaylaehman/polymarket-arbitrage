"""HourlyTempProvider: matches KXTEMP<CITY>H hourly directional temperature
markets, parses the date+hour target, and integrates a Normal(hourly_forecast,
sigma) over the market's strike interval to produce a calibrated P(YES)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from core.directional.climate.providers.hourly_temp import HourlyTempProvider


def _mkt(ticker, st, floor, cap):
    return type("M", (), {
        "ticker": ticker,
        "strike_type": st,
        "floor_strike": floor,
        "cap_strike": cap,
        "yes_price": 0.1,
    })()


def test_match_parses_date_and_hour():
    p = HourlyTempProvider().match(_mkt("KXTEMPNYCH-26JUN3017-T92.99", "greater", 92.99, None))
    assert p is not None and p.family == "hourly_temp"
    assert p.target == "2026-06-30T17" and p.geo == "KXTEMPNYCH" and p.lo == 92.99


def test_match_rejects_non_matching_ticker():
    p = HourlyTempProvider().match(_mkt("KXHIGHNY-26JUN23-T85", "greater", 85.0, None))
    assert p is None


@pytest.mark.asyncio
async def test_probability_uses_hourly_forecast():
    prov = HourlyTempProvider()
    parsed = prov.match(_mkt("KXTEMPNYCH-26JUN3017-T92.99", "greater", 92.99, None))
    with patch("core.directional.climate.providers.hourly_temp.forecast_hour",
               new=AsyncMock(return_value=85.0)):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is not None and sig.p_yes < 0.02   # P(temp>92.99) when hourly fc 85


@pytest.mark.asyncio
async def test_probability_returns_none_when_forecast_unavailable():
    prov = HourlyTempProvider()
    parsed = prov.match(_mkt("KXTEMPNYCH-26JUN3017-T92.99", "greater", 92.99, None))
    with patch("core.directional.climate.providers.hourly_temp.forecast_hour",
               new=AsyncMock(return_value=None)):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is None


@pytest.mark.asyncio
async def test_probability_swallows_exceptions():
    prov = HourlyTempProvider()
    parsed = prov.match(_mkt("KXTEMPNYCH-26JUN3017-T92.99", "greater", 92.99, None))
    with patch("core.directional.climate.providers.hourly_temp.forecast_hour",
               new=AsyncMock(side_effect=Exception("boom"))):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is None
