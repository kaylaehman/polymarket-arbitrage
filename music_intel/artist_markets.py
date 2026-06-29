"""Top-Spotify-Artist market discovery and band-based edge detection.

Discovers Polymarket 'Top Spotify Artist {year}' event markets via the Gamma
API event slug, then computes pricing edges relative to ArtistProjection
confidence bands.  Never raises — all discovery errors return [] or None.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from music_intel.sources.markets import _parse_json_list, _GAMMA_DEFAULT

logger = logging.getLogger(__name__)


@dataclass
class ArtistOutcome:
    artist: str
    pm_market_id: str   # "pm:<id>"
    yes_price: float


@dataclass
class ArtistEdge:
    artist: str
    pm_market_id: str
    side: str           # "YES" (underpriced) | "NO" (overpriced)
    model_prob: float
    prob_low: float
    prob_high: float
    market_price: float
    edge: float         # signed: model_prob - market_price
    confidence: float = 0.0   # model confidence (for downstream Kelly sizing)


def _normalize(name: str) -> str:
    """casefold + strip accents (NFKD) + collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", decomposed).strip().casefold()


async def discover_top_artist_markets(
    http: Any,
    year: str = "2026",
    gamma_url: str = _GAMMA_DEFAULT,
) -> list[ArtistOutcome]:
    """Fetch the Top-Spotify-Artist event and return one ArtistOutcome per market.

    Returns [] on any network or parsing error — never raises.
    """
    url = f"{gamma_url.rstrip('/')}/events"
    try:
        resp = await http.get(url, params={"slug": f"top-spotify-artist-{year}"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artist_markets] discovery error: %s", exc)
        return []

    events = data if isinstance(data, list) else []
    if not events:
        return []

    out: list[ArtistOutcome] = []
    for market in events[0].get("markets") or []:
        artist = market.get("groupItemTitle", "").strip()
        if not artist:
            continue
        prices = _parse_json_list(market.get("outcomePrices"))
        try:
            yes_price = float(prices[0])
        except (IndexError, TypeError, ValueError):
            continue
        market_id = f"pm:{market.get('id', '')}"
        out.append(ArtistOutcome(artist=artist, pm_market_id=market_id, yes_price=yes_price))
    return out


def compute_artist_edges(
    projections: list,
    outcomes: list[ArtistOutcome],
    *,
    min_edge: float = 0.0,
) -> list[ArtistEdge]:
    """Match projections to outcomes by normalized name and detect band violations.

    An edge fires when the market price lies OUTSIDE the model's [prob_low, prob_high]
    band AND abs(model_prob - market_price) >= min_edge.

    Returns a list sorted by abs(edge) descending, preserving outcome artist spelling.
    """
    outcome_map: dict[str, ArtistOutcome] = {_normalize(o.artist): o for o in outcomes}

    edges: list[ArtistEdge] = []
    for proj in projections:
        outcome = outcome_map.get(_normalize(proj.name))
        if outcome is None:
            continue

        price = outcome.yes_price
        prob_low = proj.prob_low
        prob_high = proj.prob_high
        model_prob = proj.prob

        if price < prob_low:
            side = "YES"
        elif price > prob_high:
            side = "NO"
        else:
            continue  # inside band — no edge

        raw_edge = model_prob - price
        if abs(raw_edge) < min_edge:
            continue

        edges.append(ArtistEdge(
            artist=outcome.artist,
            pm_market_id=outcome.pm_market_id,
            side=side,
            model_prob=model_prob,
            prob_low=prob_low,
            prob_high=prob_high,
            market_price=price,
            edge=raw_edge,
            confidence=getattr(proj, "confidence", 0.0),
        ))

    edges.sort(key=lambda e: abs(e.edge), reverse=True)
    return edges
