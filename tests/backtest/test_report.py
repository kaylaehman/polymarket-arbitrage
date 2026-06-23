import pytest
from backtest.simulate import TradeResult
from backtest.report import aggregate, sweep_params, AggResult, SweepRow


def _trade(won=True, pnl_gross=0.10, pnl_net=0.09, fee=0.01,
           entry_price_no=0.90, series="KXCPI", category="Finance",
           entry_yes_ask=0.10, days=10, vol=500.0):
    return TradeResult(
        ticker="T1", series=series, category=category,
        entry_price_no=entry_price_no, entry_yes_ask=entry_yes_ask,
        entry_day_vol=vol, days_before_close=days,
        outcome="no" if won else "yes",
        won=won, pnl_gross=pnl_gross, pnl_net=pnl_net, fee=fee,
    )


def test_aggregate_empty():
    agg = aggregate([])
    assert agg.n_trades == 0
    assert agg.ev_gross == 0.0
    assert agg.ev_net == 0.0


def test_aggregate_one_win():
    trades = [_trade(won=True, pnl_gross=0.10, pnl_net=0.09)]
    agg = aggregate(trades)
    assert agg.n_trades == 1
    assert agg.win_rate == 1.0
    assert abs(agg.ev_gross - 0.10) < 1e-6
    assert abs(agg.ev_net - 0.09) < 1e-6


def test_aggregate_mix():
    trades = [
        _trade(won=True,  pnl_gross=0.10, pnl_net=0.09),
        _trade(won=False, pnl_gross=-0.90, pnl_net=-0.91),
    ]
    agg = aggregate(trades)
    assert agg.n_trades == 2
    assert agg.win_rate == 0.5
    assert abs(agg.total_pnl_gross - (-0.80)) < 1e-6
    assert abs(agg.ev_gross - (-0.40)) < 1e-6


def test_aggregate_max_loss():
    trades = [
        _trade(won=False, pnl_net=-0.91, entry_price_no=0.90),
        _trade(won=False, pnl_net=-0.96, entry_price_no=0.95),
    ]
    agg = aggregate(trades)
    assert abs(agg.max_loss - (-0.96)) < 1e-6


def test_aggregate_by_bucket():
    # 0.90 entry → bucket "0.85-0.90"
    # 0.93 entry → bucket "0.90-0.95"
    t1 = _trade(won=True, entry_price_no=0.90, pnl_gross=0.10, pnl_net=0.09)
    t2 = _trade(won=False, entry_price_no=0.93, pnl_gross=-0.93, pnl_net=-0.94)
    agg = aggregate([t1, t2])
    assert "0.85-0.90" in agg.by_bucket or "0.90-0.95" in agg.by_bucket


def test_sweep_returns_rows():
    market = {
        "market": {
            "ticker": "KXCPI-T1", "result": "no",
            "close_time": "2024-09-13T14:00:00Z",
            "series_ticker": "KXCPI", "category": "Finance",
        },
        "candles": [{
            "end_period_ts": 1726232400 - 10 * 86400,
            "yes_ask_close": 0.10, "yes_bid_close": 0.08, "volume_fp": 500.0,
        }],
        "close_ts": 1726232400,
    }
    rows = sweep_params([market], n_values=[10], bands=[(0.05, 0.20)])
    assert len(rows) == 1
    assert rows[0].n == 10
    assert rows[0].n_trades == 1
