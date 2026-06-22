import math
import pytest
from backtest.simulate import fee_per_contract, SimParams, simulate_trades


def test_fee_rounds_up_to_cent():
    # P=0.90: 0.07 * 0.90 * 0.10 = 0.0063 → ceil to cent = 0.01
    assert fee_per_contract(0.90) == 0.01


def test_fee_midrange():
    # P=0.50: 0.07 * 0.50 * 0.50 = 0.0175 → ceil to cent = 0.02
    assert fee_per_contract(0.50) == 0.02


def test_fee_near_zero():
    # P=0.99: 0.07 * 0.99 * 0.01 = 0.000693 → ceil to cent = 0.01
    assert fee_per_contract(0.99) == 0.01


def _make_market(result="no", series="KXCPI", category="Finance"):
    return {
        "ticker": f"KXCPI-T1",
        "result": result,
        "close_time": "2024-09-13T14:00:00Z",
        "series_ticker": series,
        "category": category,
    }


def _make_candle(ts_offset_days, yes_ask=0.08, yes_bid=0.06, vol=500.0, close_ts=1726232400):
    end_ts = close_ts - ts_offset_days * 86400
    return {
        "end_period_ts": end_ts,
        "yes_ask_close": yes_ask,
        "yes_bid_close": yes_bid,
        "volume_fp": vol,
    }


def _default_params(**overrides):
    p = SimParams(
        entry_days_before_close=10,
        yes_band_lo=0.05,
        yes_band_hi=0.20,
        min_entry_volume=100.0,
        use_structural_gate=False,
        structural_min=0.0,
    )
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


def test_win_pnl_gross_and_net():
    # NO entry at yes_ask=0.10 → no_price=0.90; wins (result=no)
    # gross: 1 - 0.90 = 0.10
    # fee: ceil(0.07*0.90*0.10*100)/100 = ceil(0.063)/100 → 0.01
    # net: 0.10 - 0.01 = 0.09
    market = _make_market(result="no")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.08, vol=500.0, close_ts=close_ts)]
    params = _default_params(entry_days_before_close=10)
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert len(trades) == 1
    t = trades[0]
    assert t.won is True
    assert abs(t.entry_price_no - 0.90) < 1e-6
    assert abs(t.pnl_gross - 0.10) < 1e-6
    assert abs(t.fee - 0.01) < 1e-6
    assert abs(t.pnl_net - 0.09) < 1e-6


def test_loss_pnl():
    # YES wins → NO buyer loses entry_price_no
    market = _make_market(result="yes")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.08, vol=500.0, close_ts=close_ts)]
    params = _default_params()
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert len(trades) == 1
    t = trades[0]
    assert t.won is False
    assert abs(t.pnl_gross - (-0.90)) < 1e-6
    assert abs(t.pnl_net - (-0.90 - 0.01)) < 1e-6


def test_band_filter_excludes_yes_too_high():
    # yes_ask=0.25 is outside band [0.05, 0.20] → no trades
    market = _make_market(result="no")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.25, yes_bid=0.22, vol=500.0, close_ts=close_ts)]
    params = _default_params(yes_band_lo=0.05, yes_band_hi=0.20)
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert trades == []


def test_liquidity_gate_excludes_zero_bid():
    # yes_bid=0.0 means no two-sided book → skip
    market = _make_market(result="no")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.0, vol=500.0, close_ts=close_ts)]
    params = _default_params()
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert trades == []


def test_volume_filter_excludes_low_volume():
    market = _make_market(result="no")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.08, vol=50.0, close_ts=close_ts)]
    params = _default_params(min_entry_volume=100.0)
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert trades == []


def test_candle_selection_picks_closest_to_n_days():
    # Two candles: one 10 days before, one 15 days before; N=10 → picks 10d candle
    market = _make_market(result="no")
    close_ts = 1726232400
    candle_10d = _make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.08, vol=500.0, close_ts=close_ts)
    candle_15d = _make_candle(ts_offset_days=15, yes_ask=0.50, yes_bid=0.48, vol=500.0, close_ts=close_ts)
    params = _default_params(entry_days_before_close=10)
    trades = simulate_trades(
        [{"market": market, "candles": [candle_10d, candle_15d], "close_ts": close_ts}],
        params,
    )
    assert len(trades) == 1
    # Should use candle_10d (yes_ask=0.10), not candle_15d (yes_ask=0.50)
    assert abs(trades[0].entry_yes_ask - 0.10) < 1e-6


def test_structural_gate_filters_low_score():
    # use_structural_gate=True with structural_min=0.10; Finance+NO=0.90 scores 0.055 → skip
    market = _make_market(result="no", category="Finance")
    close_ts = 1726232400
    candles = [_make_candle(ts_offset_days=10, yes_ask=0.10, yes_bid=0.08, vol=500.0, close_ts=close_ts)]
    params = _default_params(use_structural_gate=True, structural_min=0.10)
    trades = simulate_trades([{"market": market, "candles": candles, "close_ts": close_ts}], params)
    assert trades == []
