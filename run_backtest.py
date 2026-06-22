#!/usr/bin/env python3
"""
run_backtest.py -- Collect Kalshi settled market data and simulate the
maker/longshot-NO strategy, reporting EV gross and net of fees.

Usage:
    python run_backtest.py [--series KXCPI,KXGDP]

Env required for API fetch (not needed if cache already populated):
    KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY
"""
import argparse
import asyncio
import logging
import os

from backtest.collect import collect_settled_markets, DEFAULT_SERIES
from backtest.simulate import SimParams, simulate_trades
from backtest.report import aggregate, sweep_params, format_report

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
logger = logging.getLogger("run_backtest")

CACHE_DIR = os.path.join(os.path.dirname(__file__), "backtest", "data")


def _load_kalshi_client():
    from kalshi_client.api import KalshiClient
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pk = os.environ.get("KALSHI_PRIVATE_KEY", "")
    # Support path to PEM file
    if pk and os.path.isfile(pk):
        with open(pk) as f:
            pk = f.read()
    return KalshiClient(api_key_id=key_id, private_key_pem=pk, dry_run=True)


async def _run(series_list: list[str]) -> None:
    kc = _load_kalshi_client()
    async with kc:
        markets = await collect_settled_markets(series_list, CACHE_DIR, kc)

    logger.info(f"Collected {len(markets)} total markets")

    # Default simulation params matching live bot band/N defaults
    params = SimParams(
        entry_days_before_close=10,
        yes_band_lo=0.05,
        yes_band_hi=0.20,
        min_entry_volume=100.0,
        use_structural_gate=False,
        structural_min=0.02,
    )
    trades = simulate_trades(markets, params)
    logger.info(f"Qualifying trades (default params): {len(trades)}")

    agg = aggregate(trades)

    # Param sweep across N x band
    sweep_rows = sweep_params(
        markets,
        n_values=[5, 10, 20, 30],
        bands=[
            (0.05, 0.20),
            (0.05, 0.15),
            (0.05, 0.10),
            (0.10, 0.20),
            (0.05, 0.25),
        ],
    )

    # Structural gate comparison
    params_sg = SimParams(
        entry_days_before_close=10,
        yes_band_lo=0.05,
        yes_band_hi=0.20,
        min_entry_volume=100.0,
        use_structural_gate=True,
        structural_min=0.02,
    )
    trades_sg = simulate_trades(markets, params_sg)
    agg_sg = aggregate(trades_sg)

    print(format_report(agg, sweep_rows, trades))

    print("\n--- STRUCTURAL GATE COMPARISON ---")
    print(f"  Without gate: n={agg.n_trades}, ev_net=${agg.ev_net:+.4f}")
    print(f"  With gate:    n={agg_sg.n_trades}, ev_net=${agg_sg.ev_net:+.4f}")
    gate_verdict = "HELPS" if agg_sg.ev_net > agg.ev_net else "HURTS or neutral"
    print(f"  Gate verdict: {gate_verdict}")

    print(f"\n  Total settled markets collected: {len(markets)}")
    print(f"  Markets with qualifying longshot-NO entry (default params): {len(trades)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi longshot-NO backtester")
    parser.add_argument(
        "--series",
        default=",".join(DEFAULT_SERIES),
        help="Comma-separated series tickers (default: macro set)",
    )
    args = parser.parse_args()
    series_list = [s.strip() for s in args.series.split(",") if s.strip()]
    asyncio.run(_run(series_list))


if __name__ == "__main__":
    main()
