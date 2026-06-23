import json
import os
import tempfile
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backtest.collect import collect_settled_markets, _load_cache, _save_cache


def _fake_market_dict(ticker="KXCPI-25SEP-T3", series="KXCPI", result="no"):
    return {
        "ticker": ticker,
        "result": result,
        "close_time": "2025-09-13T14:00:00Z",
        "series_ticker": series,
        "category": "Finance",
        "last_price_dollars": 0.08,
        "volume_fp": 1200.0,
        "status": "finalized",
    }


def _fake_candle(ts=1726232400, yes_ask=0.10, yes_bid=0.08, vol=500.0):
    return {
        "end_period_ts": ts,
        "yes_ask": {"close_dollars": str(yes_ask)},
        "yes_bid": {"close_dollars": str(yes_bid)},
        "volume_fp": vol,
    }


def test_cache_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        data = {
            "market": _fake_market_dict(),
            "candles": [{"end_period_ts": 1726232400, "yes_ask_close": 0.10,
                         "yes_bid_close": 0.08, "volume_fp": 500.0}],
            "close_ts": 1726232400,
        }
        _save_cache(d, "KXCPI", "KXCPI-25SEP-T3", data)
        loaded = _load_cache(d, "KXCPI", "KXCPI-25SEP-T3")
        assert loaded is not None
        assert loaded["market"]["ticker"] == "KXCPI-25SEP-T3"
        assert len(loaded["candles"]) == 1


def test_load_cache_returns_none_for_missing():
    with tempfile.TemporaryDirectory() as d:
        assert _load_cache(d, "KXCPI", "KXCPI-MISSING") is None


@pytest.mark.asyncio
async def test_collect_uses_cache_when_present():
    """If cache exists, API list_markets is never called for cached markets."""
    with tempfile.TemporaryDirectory() as d:
        cached = {
            "market": _fake_market_dict(),
            "candles": [{"end_period_ts": 1726232400, "yes_ask_close": 0.10,
                         "yes_bid_close": 0.08, "volume_fp": 500.0}],
            "close_ts": 1726232400,
        }
        _save_cache(d, "KXCPI", "KXCPI-25SEP-T3", cached)

        mock_kc = MagicMock()
        # list_markets returns empty list — if cache is used, no new items fetched
        mock_kc.list_markets = AsyncMock(return_value=([], None))

        result = await collect_settled_markets(["KXCPI"], d, mock_kc)
        assert len(result) == 1
        assert result[0]["market"]["ticker"] == "KXCPI-25SEP-T3"


@pytest.mark.asyncio
async def test_collect_fetches_and_caches_when_no_cache():
    """When cache is absent, fetches markets + candles and writes cache."""
    with tempfile.TemporaryDirectory() as d:
        from datetime import datetime, timezone

        close_dt = datetime(2025, 9, 13, 14, 0, 0, tzinfo=timezone.utc)

        mock_market = MagicMock()
        mock_market.ticker = "KXCPI-25SEP-T3"
        mock_market.series_ticker = "KXCPI"
        mock_market.result = "no"
        mock_market.close_time = close_dt
        mock_market.category = "Finance"
        mock_market.volume = 1200
        mock_market.yes_price = 0.08
        mock_market.no_price = 0.92
        mock_market.status = "finalized"

        mock_kc = MagicMock()
        mock_kc.list_markets = AsyncMock(side_effect=[
            ([mock_market], None),
        ])
        mock_kc._get = AsyncMock(return_value={
            "candlesticks": [_fake_candle()]
        })

        result = await collect_settled_markets(["KXCPI"], d, mock_kc)
        assert len(result) == 1
        # Verify cache was written
        cached = _load_cache(d, "KXCPI", "KXCPI-25SEP-T3")
        assert cached is not None
        assert cached["market"]["ticker"] == "KXCPI-25SEP-T3"
