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

# A music-chart resolution needs a music-DOMAIN anchor (a named chart/platform),
# not just a bare "#1" or "chart" — those leak in stock "price charts", "#1 seed"
# sports markets, "top Netflix movie", etc.
_MUSIC_ANCHOR = re.compile(
    r"billboard|hot ?100|billboard ?200|spotify|apple music|luminate", re.IGNORECASE,
)
# Plus a chart-position word OR a music noun, to reject e.g. "Spotify stock hits $X".
_RANK_WORD = re.compile(r"#\s*1\b|\bnumber one\b|\bno\.?\s*1\b|\btop\b", re.IGNORECASE)
_MUSIC_NOUN = re.compile(r"\bsong\b|\balbum\b|\btrack\b|\bsingle\b", re.IGNORECASE)
# Markets name the contender as "Title - Artist" in quotes (straight or curly).
_QUOTED = re.compile(r"[\"“”']([^\"“”']+)[\"“”']")


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
    """True when the question/description resolves on a MUSIC chart.

    Requires a music-domain anchor (Billboard / Spotify / Apple Music / Luminate)
    AND a chart-position word or music noun — so non-music look-alikes ("price
    chart", "#1 seed", "top global Netflix movie") are excluded.
    """
    blob = f"{question or ''} {description or ''}"
    if not _MUSIC_ANCHOR.search(blob):
        return False
    return bool(_RANK_WORD.search(blob) or _MUSIC_NOUN.search(blob))


def parse_market_target(question: Optional[str]) -> tuple[str, str]:
    """Extract the (artist, title) a market names, from a quoted ``"Title - Artist"``.

    Returns ``("", "")`` for artist-level questions with no quoted track
    (e.g. "Will Taylor Swift be #1 on the Hot 100?"). The title may contain
    spaces/punctuation; the artist is the segment after the LAST " - ".
    """
    m = _QUOTED.search(question or "")
    if not m:
        return ("", "")
    inner = m.group(1).strip()
    if " - " not in inner:
        return ("", "")
    title, _, artist = inner.rpartition(" - ")
    return (artist.strip(), title.strip())


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
            venue="polymarket", market_id=f"pm:{m.get('id', '')}", question=q,
            outcomes=[str(o) for o in _parse_json_list(m.get("outcomes"))],
            prices=prices,
            liquidity=float(m.get("liquidity") or 0.0),
            close_time=_parse_iso(m.get("endDate")),
            resolution_text=m.get("description") or "",
        ))
    return out


async def gamma_resolution(http: Any, market_id: str,
                           gamma_url: str = _GAMMA_DEFAULT) -> Optional[str]:
    """Resolution of a Polymarket market: "yes" | "no" | None (unresolved/error).

    "yes"/"no" = which named binary outcome won (outcome priced "1"). Never raises.
    """
    mid = market_id.split("pm:", 1)[1] if market_id.startswith("pm:") else market_id
    try:
        resp = await http.get(f"{gamma_url.rstrip('/')}/markets/{mid}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[markets] gamma_resolution %s error: %s", market_id, exc)
        return None
    if not data or not data.get("closed"):
        return None
    outcomes = [str(o).lower() for o in _parse_json_list(data.get("outcomes"))]
    prices = _parse_json_list(data.get("outcomePrices"))
    win_idx = None
    for i, p in enumerate(prices):
        try:
            if float(p) >= 0.99:
                win_idx = i
                break
        except (TypeError, ValueError):
            continue
    if win_idx is None or win_idx >= len(outcomes):
        return None
    won = outcomes[win_idx]
    if won in ("yes", "no"):
        return won
    return None


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
