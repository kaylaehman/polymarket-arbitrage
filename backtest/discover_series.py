"""
backtest/discover_series.py — Discover Kalshi series with qualifying longshot-NO trades.

Probes the CANDIDATE_SERIES list by default. Optionally fetches additional series
from the /series API endpoint (use --probe-api flag, but note 11k series = hours).

Saves: backtest/data/series_discovery.json  (sorted by longshot_count desc)
"""
import argparse
import asyncio
import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Curated candidate series to probe
CANDIDATE_SERIES = [
    # Crypto
    "KXBTC", "KXETH", "KXBTCR", "KXETHR",
    # Indices / equities
    "KXSPX", "KXNASDAQ", "KXDOW", "KXSPXR",
    # Jobs / macro
    "KXNFP", "KXJOBS", "KXUNEMPLOYMENT",
    # Elections / politics
    "KXSENATE", "KXHOUSE", "KXPRES", "KXGOV",
    # Weather
    "KXHURRICANE", "KXSTORM", "KXHIGHNY", "KXTEMP", "KXTEMPNY",
    # Entertainment
    "KXOSCARS", "KXEMMYS", "KXGRAMMYS",
    # Sports
    "KXNBA", "KXNFL", "KXMLB", "KXNHL",
    # CPI / Fed (already cached but validate method)
    "KXCPI", "KXFEDDECISION", "KXGDP",
]

SAMPLE_MARKETS = 20
YES_BAND_LO = 0.05
YES_BAND_HI = 0.20
MIN_VOLUME = 100.0
CANDLE_LOOKBACK_DAYS = 60
ENTRY_DAYS_BEFORE = 10


def _parse_close_ts(close_time_str: str) -> int:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return int(time.time())


def _parse_candle(raw: dict) -> dict:
    yes_ask = float((raw.get("yes_ask") or {}).get("close_dollars") or 0)
    yes_bid = float((raw.get("yes_bid") or {}).get("close_dollars") or 0)
    return {
        "end_period_ts": int(raw.get("end_period_ts", 0)),
        "yes_ask_close": yes_ask,
        "yes_bid_close": yes_bid,
        "volume_fp": float(raw.get("volume_fp", 0.0)),
    }


def _qualifies(candle: dict) -> bool:
    """Return True if a candle meets longshot-NO entry criteria."""
    return (
        candle["yes_bid_close"] > 0
        and YES_BAND_LO <= candle["yes_ask_close"] <= YES_BAND_HI
        and candle["volume_fp"] >= MIN_VOLUME
    )


def _pick_entry_candle(candles: list[dict], close_ts: int) -> Optional[dict]:
    target = close_ts - ENTRY_DAYS_BEFORE * 86400
    if not candles:
        return None
    return min(candles, key=lambda c: abs(c["end_period_ts"] - target))


async def _fetch_sample_api_series(kc, max_extra: int = 50) -> list[str]:
    """
    Fetch a small sample of series tickers from /series endpoint.
    Cap at max_extra to avoid probing 11k+ series.
    """
    tickers: list[str] = []
    try:
        data = await kc._get("/series", params={"limit": max_extra})
        series_list = (data or {}).get("series", [])
        for s in series_list:
            t = s.get("ticker") or s.get("series_ticker") or s.get("id") or ""
            if t:
                tickers.append(t)
    except Exception as exc:
        logger.warning(f"[discover] /series fetch failed: {exc}")
    logger.info(f"[discover] /series API sample returned {len(tickers)} tickers")
    return tickers


async def _score_series(kc, series: str) -> dict:
    """
    Probe up to SAMPLE_MARKETS settled markets in a series and count
    how many have qualifying longshot-NO candles ENTRY_DAYS_BEFORE close.
    Returns a dict with series, settled_count, sampled, longshot_count.
    """
    try:
        markets, _ = await kc.list_markets(
            status="settled", series_ticker=series, limit=SAMPLE_MARKETS, cursor=None
        )
    except Exception as exc:
        logger.warning(f"[discover] list_markets({series}) failed: {exc}")
        return {"series": series, "settled_count": 0, "longshot_count": 0, "error": str(exc)}
    await asyncio.sleep(0.5)

    qualified = [m for m in markets if getattr(m, "result", None) in ("yes", "no")]
    settled_count = len(qualified)
    longshot_count = 0

    for m in qualified[:SAMPLE_MARKETS]:
        close_ts = _parse_close_ts(
            m.close_time.isoformat() if hasattr(m.close_time, "isoformat") else str(m.close_time or "")
        )
        start_ts = close_ts - CANDLE_LOOKBACK_DAYS * 86400
        try:
            data = await kc._get(
                f"/series/{series}/markets/{m.ticker}/candlesticks",
                params={"period_interval": 1440, "start_ts": start_ts, "end_ts": close_ts},
            )
        except Exception as exc:
            logger.warning(f"[discover] candles({m.ticker}) failed: {exc}")
            await asyncio.sleep(1.0)
            continue
        raw_candles = (data or {}).get("candlesticks", [])
        candles = [_parse_candle(c) for c in raw_candles]
        entry = _pick_entry_candle(candles, close_ts)
        if entry and _qualifies(entry):
            longshot_count += 1
        await asyncio.sleep(1.0)

    return {
        "series": series,
        "settled_count": settled_count,
        "sampled": len(qualified[:SAMPLE_MARKETS]),
        "longshot_count": longshot_count,
    }


async def discover(output_path: str, probe_api_extra: int = 0) -> list[dict]:
    """
    Run discovery. probe_api_extra > 0 fetches that many additional API-listed
    series on top of CANDIDATE_SERIES (use sparingly; each series takes ~20s).
    """
    from kalshi_client.api import KalshiClient
    key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    pk = os.environ.get("KALSHI_PRIVATE_KEY", "")
    if pk and os.path.isfile(pk):
        with open(pk) as f:
            pk = f.read()

    all_series = list(CANDIDATE_SERIES)

    if probe_api_extra > 0:
        kc_temp = KalshiClient(api_key_id=key_id, private_key_pem=pk, dry_run=True)
        async with kc_temp:
            api_extra = await _fetch_sample_api_series(kc_temp, max_extra=probe_api_extra)
        known = set(all_series)
        for s in api_extra:
            if s not in known:
                all_series.append(s)
                known.add(s)

    logger.info(f"[discover] probing {len(all_series)} series total")

    results: list[dict] = []
    kc = KalshiClient(api_key_id=key_id, private_key_pem=pk, dry_run=True)
    async with kc:
        for i, series in enumerate(all_series):
            logger.info(f"[discover] [{i+1}/{len(all_series)}] scoring {series}")
            row = await _score_series(kc, series)
            results.append(row)

    results.sort(key=lambda r: r["longshot_count"], reverse=True)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n=== Series Discovery — Top 15 by longshot_count ===")
    for row in results[:15]:
        print(f"  {row['series']:20s}  longshot={row['longshot_count']:3d}  settled={row['settled_count']:4d}")
    print(f"\nFull results saved to {output_path}")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Discover Kalshi series with longshot-NO trades")
    parser.add_argument("--probe-api-extra", type=int, default=0,
                        help="Fetch this many extra series from /series API (default: 0 = candidates only)")
    args = parser.parse_args()
    base = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(base, "data", "series_discovery.json")
    asyncio.run(discover(out, probe_api_extra=args.probe_api_extra))
