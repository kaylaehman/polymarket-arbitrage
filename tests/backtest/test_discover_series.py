"""
tests/backtest/test_discover_series.py — Unit tests for backtest/discover_series.py.

All KalshiClient calls are mocked; no network access.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from backtest.discover_series import (
    _parse_close_ts,
    _parse_candle,
    _qualifies,
    _pick_entry_candle,
    ENTRY_DAYS_BEFORE,
    YES_BAND_LO,
    YES_BAND_HI,
    MIN_VOLUME,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _candle(yes_ask: float, yes_bid: float, vol: float, ts: int = 1_700_000_000) -> dict:
    return {"end_period_ts": ts, "yes_ask_close": yes_ask, "yes_bid_close": yes_bid, "volume_fp": vol}


def _raw_candle(yes_ask: float, yes_bid: float, vol: float, ts: int = 1_700_000_000) -> dict:
    return {
        "end_period_ts": ts,
        "yes_ask": {"close_dollars": str(yes_ask)},
        "yes_bid": {"close_dollars": str(yes_bid)},
        "volume_fp": vol,
    }


# ── _parse_close_ts ───────────────────────────────────────────────────────────

def test_parse_close_ts_iso():
    ts = _parse_close_ts("2025-09-13T14:00:00Z")
    assert ts == int(datetime(2025, 9, 13, 14, 0, 0, tzinfo=timezone.utc).timestamp())


def test_parse_close_ts_with_offset():
    ts = _parse_close_ts("2025-09-13T14:00:00+00:00")
    assert ts == int(datetime(2025, 9, 13, 14, 0, 0, tzinfo=timezone.utc).timestamp())


def test_parse_close_ts_bad_falls_back_to_now():
    import time
    before = int(time.time())
    ts = _parse_close_ts("not-a-date")
    after = int(time.time())
    assert before <= ts <= after


# ── _parse_candle ─────────────────────────────────────────────────────────────

def test_parse_candle_normal():
    raw = _raw_candle(0.12, 0.09, 500.0, ts=1_700_000_000)
    result = _parse_candle(raw)
    assert result["yes_ask_close"] == pytest.approx(0.12)
    assert result["yes_bid_close"] == pytest.approx(0.09)
    assert result["volume_fp"] == pytest.approx(500.0)
    assert result["end_period_ts"] == 1_700_000_000


def test_parse_candle_missing_fields():
    result = _parse_candle({})
    assert result["yes_ask_close"] == 0.0
    assert result["yes_bid_close"] == 0.0
    assert result["volume_fp"] == 0.0


def test_parse_candle_null_nested():
    result = _parse_candle({"yes_ask": None, "yes_bid": None, "volume_fp": 200.0})
    assert result["yes_ask_close"] == 0.0
    assert result["yes_bid_close"] == 0.0
    assert result["volume_fp"] == 200.0


# ── _qualifies ────────────────────────────────────────────────────────────────

def test_qualifies_happy_path():
    c = _candle(yes_ask=0.10, yes_bid=0.08, vol=500.0)
    assert _qualifies(c) is True


def test_qualifies_rejects_zero_bid():
    c = _candle(yes_ask=0.10, yes_bid=0.0, vol=500.0)
    assert _qualifies(c) is False


def test_qualifies_rejects_below_band():
    c = _candle(yes_ask=YES_BAND_LO - 0.01, yes_bid=0.01, vol=500.0)
    assert _qualifies(c) is False


def test_qualifies_rejects_above_band():
    c = _candle(yes_ask=YES_BAND_HI + 0.01, yes_bid=0.15, vol=500.0)
    assert _qualifies(c) is False


def test_qualifies_rejects_low_volume():
    c = _candle(yes_ask=0.10, yes_bid=0.08, vol=MIN_VOLUME - 1)
    assert _qualifies(c) is False


def test_qualifies_at_band_boundary():
    lo = _candle(yes_ask=YES_BAND_LO, yes_bid=0.01, vol=MIN_VOLUME)
    hi = _candle(yes_ask=YES_BAND_HI, yes_bid=0.15, vol=MIN_VOLUME)
    assert _qualifies(lo) is True
    assert _qualifies(hi) is True


# ── _pick_entry_candle ────────────────────────────────────────────────────────

def test_pick_entry_candle_selects_nearest():
    close_ts = 1_700_000_000
    target = close_ts - ENTRY_DAYS_BEFORE * 86400
    near = _candle(0.10, 0.08, 200.0, ts=target + 3600)
    far = _candle(0.12, 0.09, 300.0, ts=target - 86400 * 5)
    result = _pick_entry_candle([far, near], close_ts)
    assert result is near


def test_pick_entry_candle_empty_returns_none():
    assert _pick_entry_candle([], 1_700_000_000) is None


def test_pick_entry_candle_single():
    close_ts = 1_700_000_000
    c = _candle(0.10, 0.08, 200.0, ts=close_ts - ENTRY_DAYS_BEFORE * 86400)
    assert _pick_entry_candle([c], close_ts) is c


# ── _score_series (integration mock) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_score_series_counts_longshots():
    from datetime import datetime, timezone
    from backtest.discover_series import _score_series

    close_dt = datetime(2025, 9, 13, 14, 0, 0, tzinfo=timezone.utc)
    close_ts = int(close_dt.timestamp())
    target_ts = close_ts - ENTRY_DAYS_BEFORE * 86400

    mock_market = MagicMock()
    mock_market.ticker = "KXHIGHNY-25SEP-T80"
    mock_market.result = "no"
    mock_market.close_time = close_dt

    mock_kc = MagicMock()
    mock_kc.list_markets = AsyncMock(return_value=([mock_market], None))
    mock_kc._get = AsyncMock(return_value={
        "candlesticks": [{
            "end_period_ts": target_ts,
            "yes_ask": {"close_dollars": "0.10"},
            "yes_bid": {"close_dollars": "0.08"},
            "volume_fp": 500.0,
        }]
    })

    result = await _score_series(mock_kc, "KXHIGHNY")
    assert result["longshot_count"] == 1
    assert result["settled_count"] == 1


@pytest.mark.asyncio
async def test_score_series_handles_api_error():
    from backtest.discover_series import _score_series

    mock_kc = MagicMock()
    mock_kc.list_markets = AsyncMock(side_effect=Exception("timeout"))

    result = await _score_series(mock_kc, "KXBAD")
    assert result["longshot_count"] == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_score_series_skips_unqualified():
    """Markets with yes_ask > YES_BAND_HI should not count."""
    from datetime import datetime, timezone
    from backtest.discover_series import _score_series

    close_dt = datetime(2025, 9, 13, 14, 0, 0, tzinfo=timezone.utc)
    close_ts = int(close_dt.timestamp())
    target_ts = close_ts - ENTRY_DAYS_BEFORE * 86400

    mock_market = MagicMock()
    mock_market.ticker = "KXBTC-25SEP-T60000"
    mock_market.result = "no"
    mock_market.close_time = close_dt

    mock_kc = MagicMock()
    mock_kc.list_markets = AsyncMock(return_value=([mock_market], None))
    mock_kc._get = AsyncMock(return_value={
        "candlesticks": [{
            "end_period_ts": target_ts,
            "yes_ask": {"close_dollars": "0.90"},   # too high
            "yes_bid": {"close_dollars": "0.85"},
            "volume_fp": 5000.0,
        }]
    })

    result = await _score_series(mock_kc, "KXBTC")
    assert result["longshot_count"] == 0
