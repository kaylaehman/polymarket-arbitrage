import pytest
from unittest.mock import AsyncMock, patch
from core.directional.climate.providers.high_temp import HighTempProvider


def _mkt(ticker, st, floor, cap):
    return type("M", (), {"ticker": ticker, "strike_type": st, "floor_strike": floor,
                          "cap_strike": cap, "yes_price": 0.1, "category": "Climate and Weather"})()


def test_match_high_temp_bucket():
    p = HighTempProvider().match(_mkt("KXHIGHNY-26JUL01-B98.5", "between", 98.0, 99.0))
    assert p is not None and p.family == "high_temp" and p.lo == 98.0 and p.hi == 99.0
    assert p.target == "2026-07-01"


def test_match_rejects_non_high():
    assert HighTempProvider().match(_mkt("KXTORNADO-26JUN-425", "greater", 425.0, None)) is None


@pytest.mark.asyncio
async def test_probability_far_above_forecast_is_low():
    prov = HighTempProvider()
    parsed = prov.match(_mkt("KXHIGHNY-26JUL01-T99", "greater", 99.0, None))
    with patch("core.directional.climate.providers.high_temp.forecast_high",
               new=AsyncMock(return_value=88.0)):
        sig = await prov.probability(parsed, http=None, ctx={})
    assert sig is not None and sig.p_yes < 0.01   # P(high>99) when forecast 88
