"""
backtest/simulate.py — Pure simulation of the maker/longshot-NO strategy.

No I/O; all data is passed in as plain dicts/dataclasses.
"""
import math
from dataclasses import dataclass
from typing import Optional

from utils.structural_bias import structural_score


@dataclass
class SimParams:
    entry_days_before_close: int = 10
    yes_band_lo: float = 0.05
    yes_band_hi: float = 0.20
    min_entry_volume: float = 100.0
    use_structural_gate: bool = False
    structural_min: float = 0.02


@dataclass
class TradeResult:
    ticker: str
    series: str
    category: str
    entry_price_no: float       # 1 - yes_ask_close
    entry_yes_ask: float
    entry_day_vol: float
    days_before_close: int
    outcome: str                # "yes" or "no"
    won: bool
    pnl_gross: float
    pnl_net: float
    fee: float


def fee_per_contract(p: float) -> float:
    """Kalshi fee: ceil(0.07 * P * (1-P)) rounded up to the nearest cent."""
    raw = 0.07 * p * (1.0 - p)
    return math.ceil(raw * 100) / 100.0


def _parse_close_ts(close_time_str: str) -> int:
    """Parse ISO close_time to unix timestamp (seconds)."""
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def _select_entry_candle(candles: list[dict], target_ts: int) -> Optional[dict]:
    """Return the candle whose end_period_ts is closest to target_ts."""
    if not candles:
        return None
    return min(candles, key=lambda c: abs(c["end_period_ts"] - target_ts))


def simulate_trades(markets_with_candles: list[dict], params: SimParams) -> list[TradeResult]:
    """
    Simulate NO-side entries for every qualifying market.

    Args:
        markets_with_candles: list of dicts with keys:
            "market": dict (ticker, result, close_time, series_ticker, category)
            "candles": list[dict] (end_period_ts, yes_ask_close, yes_bid_close, volume_fp)
            "close_ts": int (unix timestamp; precomputed from close_time)
        params: SimParams

    Returns:
        list of TradeResult, one per qualifying entry.
    """
    results: list[TradeResult] = []

    for item in markets_with_candles:
        market = item["market"]
        candles = item["candles"]
        close_ts = item["close_ts"]

        target_ts = close_ts - params.entry_days_before_close * 86400
        candle = _select_entry_candle(candles, target_ts)
        if candle is None:
            continue

        yes_ask = candle["yes_ask_close"]
        yes_bid = candle["yes_bid_close"]
        vol = candle["volume_fp"]

        # Liquidity gate: no real two-sided book
        if yes_bid <= 0:
            continue

        # Band filter
        if not (params.yes_band_lo <= yes_ask <= params.yes_band_hi):
            continue

        # Volume gate
        if vol < params.min_entry_volume:
            continue

        no_entry_price = 1.0 - yes_ask

        # Structural gate
        if params.use_structural_gate:
            score = structural_score(no_entry_price, "NO", market.get("category", ""))
            if score < params.structural_min:
                continue

        outcome = market["result"]
        won = outcome == "no"
        fee = fee_per_contract(no_entry_price)

        if won:
            pnl_gross = 1.0 - no_entry_price
        else:
            pnl_gross = -no_entry_price

        pnl_net = pnl_gross - fee

        results.append(TradeResult(
            ticker=market["ticker"],
            series=market["series_ticker"],
            category=market.get("category", ""),
            entry_price_no=no_entry_price,
            entry_yes_ask=yes_ask,
            entry_day_vol=vol,
            days_before_close=params.entry_days_before_close,
            outcome=outcome,
            won=won,
            pnl_gross=pnl_gross,
            pnl_net=pnl_net,
            fee=fee,
        ))

    return results
