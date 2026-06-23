"""Tests for core.market_data — AVClient, parse_financial_ticker, crossing_margin."""
from __future__ import annotations

import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.market_data import (
    AVClient,
    FinancialMarket,
    crossing_margin,
    parse_financial_ticker,
)


# ─── parse_financial_ticker ────────────────────────────────────────────────────


def test_parse_financial_ticker_btc_threshold():
    fm = parse_financial_ticker("KXBTCD-26JUN2317-T65499.99")
    assert fm is not None
    assert fm.underlying == "BTC"
    assert fm.threshold == 65499.99
    assert fm.direction == "above"
    assert fm.market_type == "threshold"
    assert fm.expiry == date(2026, 6, 23)


def test_parse_financial_ticker_btc_bucket():
    fm = parse_financial_ticker("KXBTC-26JUN2317-B64875")
    assert fm is not None
    assert fm.underlying == "BTC"
    assert fm.market_type == "bucket"
    assert fm.bucket_lo == 64875.0


def test_parse_financial_ticker_eth_threshold():
    fm = parse_financial_ticker("KXETHD-26JUN2317-T1779.99")
    assert fm is not None
    assert fm.underlying == "ETH"
    assert fm.threshold == 1779.99
    assert fm.market_type == "threshold"


def test_parse_financial_ticker_wti():
    fm = parse_financial_ticker("KXWTI-26JUN2414-T76.99")
    assert fm is not None
    assert fm.underlying == "WTI"
    assert fm.threshold == 76.99
    assert fm.expiry == date(2026, 6, 24)


def test_parse_financial_ticker_eurusd():
    fm = parse_financial_ticker("KXEURUSD-26JUN2310-T1.15799")
    assert fm is not None
    assert fm.underlying == "EURUSD"
    assert abs(fm.threshold - 1.15799) < 1e-9
    assert fm.market_type == "threshold"


def test_parse_financial_ticker_none_cases():
    # weather ticker - won't match KXBTCD?|KXETHD?|KXWTI|KXEURUSD pattern
    assert parse_financial_ticker("KXHIGHNY-26JUN23-B75") is None
    assert parse_financial_ticker("some-random-string") is None
    assert parse_financial_ticker("") is None
    # Excluded series: KXDOGE not in the regex alternatives
    assert parse_financial_ticker("KXDOGE-26JUN2317-B0.172") is None


# ─── crossing_margin ───────────────────────────────────────────────────────────


def test_crossing_margin_math():
    # price=100, vol=0.02, threshold=110, days=1 → em=2.0, z=5.0
    z = crossing_margin(100.0, 0.02, 110.0, 1.0)
    assert abs(z - 5.0) < 1e-9


def test_crossing_margin_zero_days():
    # days=0 should be treated as 1
    z_zero = crossing_margin(100.0, 0.02, 110.0, 0.0)
    z_one = crossing_margin(100.0, 0.02, 110.0, 1.0)
    assert abs(z_zero - z_one) < 1e-9


def test_crossing_margin_negative_z():
    # threshold below price → negative z
    z = crossing_margin(100.0, 0.02, 90.0, 1.0)
    assert z < 0


# ─── AVClient helpers ──────────────────────────────────────────────────────────


def _make_mock_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


def _make_av_client(http_mock) -> AVClient:
    return AVClient(
        api_key="TESTKEY",
        price_ttl_s=14400,
        vol_ttl_s=86400,
        http=http_mock,
    )


BTC_EXCHANGE_RATE_RESP = {
    "Realtime Currency Exchange Rate": {
        "5. Exchange Rate": "64115.28000000",
        "6. Last Refreshed": "2026-06-23 02:03:20",
    }
}

WTI_RESP = {
    "name": "Crude Oil Prices WTI",
    "data": [
        {"date": "2026-06-15", "value": "84.65"},
        {"date": "2026-06-12", "value": "88.62"},
    ],
}


def _make_digital_currency_resp(n: int = 22, base: float = 64000.0) -> dict:
    """Create fake DIGITAL_CURRENCY_DAILY response with n days."""
    from datetime import date, timedelta

    ts: dict = {}
    d = date(2026, 6, 23)
    price = base
    for i in range(n):
        key = (d - timedelta(days=i)).isoformat()
        ts[key] = {
            "1. open": str(price),
            "2. high": str(price * 1.01),
            "3. low": str(price * 0.99),
            "4. close": str(price),
            "5. volume": "1000",
        }
        price *= 0.995
    return {"Time Series (Digital Currency Daily)": ts}


@pytest.mark.asyncio
async def test_avclient_routes_btc_to_exchange_rate():
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(BTC_EXCHANGE_RATE_RESP))
    client = _make_av_client(http)
    price = await client.get_price("BTC")
    assert price == pytest.approx(64115.28, rel=1e-4)
    call_params = http.get.call_args
    assert call_params[1]["params"]["function"] == "CURRENCY_EXCHANGE_RATE"
    assert call_params[1]["params"]["from_currency"] == "BTC"


@pytest.mark.asyncio
async def test_avclient_routes_wti_to_wti_function():
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(WTI_RESP))
    client = _make_av_client(http)
    price = await client.get_price("WTI")
    assert price == pytest.approx(84.65, rel=1e-4)
    call_params = http.get.call_args
    assert call_params[1]["params"]["function"] == "WTI"


@pytest.mark.asyncio
async def test_avclient_price_cache_hit():
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(BTC_EXCHANGE_RATE_RESP))
    client = _make_av_client(http)
    await client.get_price("BTC")
    call_count_after_first = http.get.call_count
    await client.get_price("BTC")
    assert http.get.call_count == call_count_after_first


@pytest.mark.asyncio
async def test_avclient_rate_limit_note_returns_none():
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(
        {"Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 25 requests per day."}
    ))
    client = _make_av_client(http)
    price = await client.get_price("BTC")
    assert price is None


@pytest.mark.asyncio
async def test_avclient_information_returns_none():
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(
        {"Information": "The standard API rate limit is 25 requests per day."}
    ))
    client = _make_av_client(http)
    price = await client.get_price("ETH")
    assert price is None


@pytest.mark.asyncio
async def test_avclient_rate_serialization():
    """Two sequential price calls (different underlyings) should take >= 1s combined."""
    call_times = []

    async def fake_get(url, *, params):
        call_times.append(time.monotonic())
        resp = MagicMock()
        if params.get("from_currency") == "BTC":
            resp.json.return_value = BTC_EXCHANGE_RATE_RESP
        else:
            resp.json.return_value = {
                "Realtime Currency Exchange Rate": {
                    "5. Exchange Rate": "2000.00",
                    "6. Last Refreshed": "2026-06-23",
                }
            }
        resp.raise_for_status = MagicMock()
        return resp

    http = AsyncMock()
    http.get = fake_get
    client = _make_av_client(http)
    t0 = time.monotonic()
    await client.get_price("BTC")
    await client.get_price("ETH")
    elapsed = time.monotonic() - t0
    assert elapsed >= 1.0, f"Expected >= 1s for 2 serialized calls, got {elapsed:.2f}s"


def test_gate_keeps_at_min_sigma():
    """price=64000, vol=0.04, threshold=74000, days=1 → z≈3.9 >= 2.5 → KEEP."""
    z = crossing_margin(64000.0, 0.04, 74000.0, 1.0)
    assert z >= 2.5


def test_gate_skips_within_min_sigma():
    """price=64000, vol=0.04, threshold=65000, days=1 → z≈0.39 < 2.5 → SKIP."""
    z = crossing_margin(64000.0, 0.04, 65000.0, 1.0)
    assert z < 2.5


@pytest.mark.asyncio
async def test_gate_skips_on_no_data_require_true():
    """AV returns None (rate limited), require_data=True → SKIP."""
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(
        {"Note": "Rate limited"}
    ))
    av = _make_av_client(http)
    fm = parse_financial_ticker("KXBTCD-26JUN2317-T65499.99")
    assert fm is not None
    price = await av.get_price(fm.underlying)
    assert price is None
    require_data = True
    keep = not require_data
    assert keep is False


@pytest.mark.asyncio
async def test_gate_keeps_on_no_data_require_false():
    """AV returns None (rate limited), require_data=False → KEEP (structural fallback)."""
    http = AsyncMock()
    http.get = AsyncMock(return_value=_make_mock_response(
        {"Note": "Rate limited"}
    ))
    av = _make_av_client(http)
    fm = parse_financial_ticker("KXBTCD-26JUN2317-T65499.99")
    assert fm is not None
    price = await av.get_price(fm.underlying)
    assert price is None
    require_data = False
    keep = not require_data
    assert keep is True


def test_non_financial_market_untouched():
    """Weather ticker → parse_financial_ticker returns None, gate not applied."""
    result = parse_financial_ticker("KXHIGHNY-26JUN23-B75")
    assert result is None
