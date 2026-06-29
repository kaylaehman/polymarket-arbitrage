"""Market discovery — find open Polymarket/Kalshi markets that resolve on music
charts. "No matching market" is a NORMAL outcome: logged at INFO, never raised.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_GAMMA_DEFAULT = "https://gamma-api.polymarket.com"

MUSIC_KEYWORDS = re.compile(
    r"billboard|hot ?100|billboard ?200|number one|#1|chart|spotify|apple music|"
    r"album of|song of",
    re.IGNORECASE,
)


@dataclass
class MarketCandidate:
    venue: str               # "polymarket" | "kalshi"
    market_id: str
    question: str
    outcomes: list[str]
    prices: list[float]
    liquidity: float
    close_time: Optional[datetime]
    resolution_text: str


def is_music_market(question: Optional[str], description: Optional[str] = None) -> bool:
    """True when the question/description mentions a music-chart resolution."""
    blob = f"{question or ''} {description or ''}"
    return bool(MUSIC_KEYWORDS.search(blob))


def _parse_json_list(val: Any) -> list:
    """Gamma encodes outcomes/prices as JSON strings, e.g. '["Yes","No"]'."""
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            out = json.loads(val)
            return out if isinstance(out, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _parse_iso(val: Any) -> Optional[datetime]:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None


async def discover_polymarket(http: Any, gamma_url: str = _GAMMA_DEFAULT) -> list[MarketCandidate]:
    """Open Polymarket music markets via the Gamma API. [] on any error."""
    try:
        resp = await http.get(
            f"{gamma_url.rstrip('/')}/markets",
            params={"closed": "false", "limit": 200, "order": "volume", "ascending": "false"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[markets] polymarket discovery error: %s", exc)
        return []

    arr = data if isinstance(data, list) else data.get("data", data.get("markets", []))
    out: list[MarketCandidate] = []
    for m in arr or []:
        q = m.get("question") or ""
        if not is_music_market(q, m.get("description")):
            continue
        prices_raw = _parse_json_list(m.get("outcomePrices"))
        prices: list[float] = []
        for p in prices_raw:
            try:
                prices.append(float(p))
            except (TypeError, ValueError):
                prices.append(0.0)
        out.append(MarketCandidate(
            venue="polymarket", market_id=str(m.get("id", "")), question=q,
            outcomes=[str(o) for o in _parse_json_list(m.get("outcomes"))],
            prices=prices,
            liquidity=float(m.get("liquidity") or 0.0),
            close_time=_parse_iso(m.get("endDate")),
            resolution_text=m.get("description") or "",
        ))
    return out


async def discover_kalshi(kalshi_client: Any) -> list[MarketCandidate]:
    """Open Kalshi music markets (RARE — empty is normal). [] on any error."""
    if kalshi_client is None:
        return []
    try:
        markets, _ = await kalshi_client.list_markets(status="open", limit=1000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[markets] kalshi discovery error: %s", exc)
        return []

    out: list[MarketCandidate] = []
    for m in markets or []:
        if not is_music_market(getattr(m, "title", ""), getattr(m, "subtitle", "")):
            continue
        yes = float(getattr(m, "yes_price", 0.0) or 0.0)
        out.append(MarketCandidate(
            venue="kalshi", market_id=f"kalshi:{m.ticker}", question=m.title,
            outcomes=["Yes", "No"], prices=[yes, round(1.0 - yes, 4)],
            liquidity=float(getattr(m, "volume", 0) or 0),
            close_time=getattr(m, "close_time", None),
            resolution_text=getattr(m, "subtitle", ""),
        ))
    return out


async def discover_all(http: Any, kalshi_client: Any = None, gamma_url: str = _GAMMA_DEFAULT) -> list[MarketCandidate]:
    """All open music markets across venues. 0 results is a clean, normal outcome."""
    candidates = await discover_polymarket(http, gamma_url=gamma_url)
    if kalshi_client is not None:
        candidates += await discover_kalshi(kalshi_client)
    logger.info("[markets] music market discovery: %d candidate(s)", len(candidates))
    return candidates
