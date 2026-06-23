"""
backtest/collect.py — Fetch settled Kalshi markets + daily candlesticks.

Caches raw results to disk (backtest/data/{series}/{ticker}.json) so
re-runs never re-hit the API.  Pacing: 0.5-1.0s between candlestick calls.
"""
import asyncio
import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_SERIES = [
    # Original macro set
    "KXCPI",
    "KXCPIYOY",
    "KXCPICORE",
    "KXCPICOREYOY",
    "KXPCECORE",
    "KXGDP",
    "KXFEDDECISION",
    # Added via series discovery (longshot_count > 0 in sampling run)
    "KXHIGHNY",   # NY daily high temp — longshot=6/20
    "KXNHL",      # NHL team props — longshot=6/20
    "KXNBA",      # NBA team props — longshot=4/18
]


def _cache_path(cache_dir: str, series: str, ticker: str) -> str:
    series_dir = os.path.join(cache_dir, series)
    os.makedirs(series_dir, exist_ok=True)
    return os.path.join(series_dir, f"{ticker}.json")


def _load_cache(cache_dir: str, series: str, ticker: str) -> Optional[dict]:
    path = _cache_path(cache_dir, series, ticker)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _save_cache(cache_dir: str, series: str, ticker: str, data: dict) -> None:
    path = _cache_path(cache_dir, series, ticker)
    with open(path, "w") as f:
        json.dump(data, f)


def _market_to_dict(m) -> dict:
    """Convert KalshiMarket object to plain dict for JSON serialization."""
    close_time_str = None
    if m.close_time:
        if hasattr(m.close_time, "isoformat"):
            close_time_str = m.close_time.isoformat()
        else:
            close_time_str = str(m.close_time)
    return {
        "ticker": m.ticker,
        "result": m.result,
        "close_time": close_time_str,
        "series_ticker": m.series_ticker,
        "category": m.category,
        "volume_fp": float(m.volume) if m.volume else 0.0,
        "last_price_dollars": m.yes_price,
        "status": m.status,
    }


def _parse_candle(raw: dict) -> dict:
    """Normalize a raw candlestick dict to our internal shape."""
    yes_ask_close = 0.0
    yes_bid_close = 0.0
    if raw.get("yes_ask") and raw["yes_ask"].get("close_dollars"):
        yes_ask_close = float(raw["yes_ask"]["close_dollars"])
    if raw.get("yes_bid") and raw["yes_bid"].get("close_dollars"):
        yes_bid_close = float(raw["yes_bid"]["close_dollars"])
    return {
        "end_period_ts": int(raw.get("end_period_ts", 0)),
        "yes_ask_close": yes_ask_close,
        "yes_bid_close": yes_bid_close,
        "volume_fp": float(raw.get("volume_fp", 0.0)),
    }


async def _fetch_candlesticks(kc, series_ticker: str, ticker: str, start_ts: int, end_ts: int) -> list[dict]:
    """Fetch daily candles for a single market. Returns normalized candle dicts."""
    try:
        data = await kc._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={
                "period_interval": 1440,
                "start_ts": start_ts,
                "end_ts": end_ts,
            },
        )
    except Exception as exc:
        logger.warning(f"[collect] candlestick fetch failed for {ticker}: {exc}")
        return []

    raw_candles = data.get("candlesticks", []) if data else []
    return [_parse_candle(c) for c in raw_candles]


def _close_ts_from_dict(m_dict: dict) -> int:
    """Extract unix timestamp from close_time string."""
    close_str = m_dict.get("close_time", "")
    if not close_str:
        return int(time.time())
    try:
        dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


async def collect_settled_markets(
    series_list: list[str],
    cache_dir: str,
    kc,
) -> list[dict]:
    """
    Collect settled markets + daily candlesticks for the given series.

    Returns list of:
        {"market": dict, "candles": list[dict], "close_ts": int}

    Results are loaded from cache when available; only missing entries hit the API.
    Candlestick calls are paced 0.5-1.0s apart.
    """
    results: list[dict] = []
    cached_tickers: set[str] = set()

    # First pass: load from cache by scanning cache_dir subdirs
    for series in series_list:
        series_dir = os.path.join(cache_dir, series)
        if os.path.isdir(series_dir):
            for fname in os.listdir(series_dir):
                if fname.endswith(".json"):
                    ticker = fname[:-5]
                    data = _load_cache(cache_dir, series, ticker)
                    if data:
                        # Ensure series_ticker is populated from the directory name
                        if not data["market"].get("series_ticker"):
                            data["market"]["series_ticker"] = series
                        results.append(data)
                        cached_tickers.add(ticker)

    logger.info(f"[collect] loaded {len(cached_tickers)} markets from cache")

    # Second pass: fetch missing from API
    for series in series_list:
        cursor = None
        series_markets = []
        while True:
            try:
                markets, next_cursor = await kc.list_markets(
                    status="settled",
                    series_ticker=series,
                    limit=1000,
                    cursor=cursor,
                )
            except Exception as exc:
                logger.warning(f"[collect] list_markets failed for {series}: {exc}")
                break

            for m in markets:
                if m.ticker not in cached_tickers and m.result in ("yes", "no"):
                    series_markets.append(m)

            if not next_cursor:
                break
            cursor = next_cursor

        logger.info(f"[collect] series {series}: {len(series_markets)} new markets to fetch")

        for m in series_markets:
            m_dict = _market_to_dict(m)
            # Force series_ticker from the known series context (API may return "")
            if not m_dict.get("series_ticker"):
                m_dict["series_ticker"] = series
            close_ts = _close_ts_from_dict(m_dict)
            start_ts = close_ts - 60 * 86400  # 60 days before close
            end_ts = close_ts

            candles = await _fetch_candlesticks(kc, series, m.ticker, start_ts, end_ts)

            entry = {"market": m_dict, "candles": candles, "close_ts": close_ts}
            _save_cache(cache_dir, series, m.ticker, entry)
            results.append(entry)
            cached_tickers.add(m.ticker)

            # Pace between candlestick calls to avoid 429
            await asyncio.sleep(random.uniform(0.5, 1.0))

    logger.info(f"[collect] total markets collected: {len(results)}")
    return results
