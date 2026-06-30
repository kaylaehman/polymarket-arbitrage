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
from typing import Any, Optional

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


async def discover_artist_market(
    http: Any,
    slug: str,
    gamma_url: str = _GAMMA_DEFAULT,
) -> list[ArtistOutcome]:
    """Fetch a Polymarket event by exact slug and return one ArtistOutcome per market.

    Args:
        http: An httpx.AsyncClient (or compatible mock).
        slug: The Gamma API event slug (e.g. "top-spotify-artist-2026").
        gamma_url: Gamma API base URL.

    Returns:
        List of ArtistOutcome; [] on any network or parsing error — never raises.
    """
    url = f"{gamma_url.rstrip('/')}/events"
    try:
        resp = await http.get(url, params={"slug": slug})
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


_GAMMA_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"


async def find_artist_event_slug(
    http: Any,
    query: str,
    gamma_url: str = _GAMMA_DEFAULT,
) -> Optional[str]:
    """Search Gamma for an open event whose title contains all tokens of `query`.

    Returns the first matching open event's slug, or None. Never raises.
    """
    tokens = [t.casefold() for t in query.split()]
    search_url = f"{gamma_url.rstrip('/')}/public-search"
    try:
        resp = await http.get(search_url, params={"q": query, "limit_per_type": 10})
        resp.raise_for_status()
        events = resp.json().get("events", [])
        for event in events:
            if event.get("closed"):
                continue
            title = event.get("title", "").casefold()
            if all(tok in title for tok in tokens):
                return event.get("slug")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[artist_markets] search error: %s", exc)
    return None


async def discover_artist_market_by_search(
    http: Any,
    query: str,
    gamma_url: str = _GAMMA_DEFAULT,
) -> list[ArtistOutcome]:
    """Search for an open event by query, then fetch its outcomes.

    Returns [] if no matching event is found or on any error. Never raises.
    """
    slug = await find_artist_event_slug(http, query, gamma_url)
    if slug is None:
        return []
    return await discover_artist_market(http, slug, gamma_url)


async def discover_top_artist_markets(
    http: Any,
    year: str = "2026",
    gamma_url: str = _GAMMA_DEFAULT,
) -> list[ArtistOutcome]:
    """Fetch the Top-Spotify-Artist event and return one ArtistOutcome per market.

    Thin wrapper around discover_artist_market using the standard slug pattern.
    Returns [] on any network or parsing error — never raises.
    """
    return await discover_artist_market(http, f"top-spotify-artist-{year}", gamma_url)


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


def compute_rank_edges(
    rank_probs: dict,
    outcomes: list[ArtistOutcome],
    rank: int,
    *,
    min_edge: float = 0.10,
) -> list[ArtistEdge]:
    """Edges for a '#<rank> Spotify Artist' market using Monte-Carlo rank probabilities.

    For each outcome, looks up the artist's P(rank=k) from rank_probs. Emits an
    ArtistEdge when abs(model_prob - yes_price) >= min_edge. Name matching uses
    _normalize for accent/case tolerance.

    Args:
        rank_probs: {artist_name: {rank:int -> prob:float}} from rank_probabilities().
        outcomes: ArtistOutcome list from discover_artist_market().
        rank: The rank to evaluate (1-based, e.g. 2 for '#2 Spotify Artist').
        min_edge: Minimum absolute edge to emit a signal.

    Returns:
        List of ArtistEdge sorted by abs(edge) descending.
    """
    # Build normalized lookup: normalized_name -> (canonical_name, rank_dict)
    norm_probs: dict[str, tuple[str, dict[int, float]]] = {
        _normalize(name): (name, rank_dict)
        for name, rank_dict in rank_probs.items()
    }

    edges: list[ArtistEdge] = []
    for outcome in outcomes:
        entry = norm_probs.get(_normalize(outcome.artist))
        p = entry[1].get(rank, 0.0) if entry is not None else 0.0
        yes_price = outcome.yes_price
        raw_edge = p - yes_price
        if abs(raw_edge) < min_edge:
            continue
        side = "YES" if raw_edge > 0 else "NO"
        edges.append(ArtistEdge(
            artist=outcome.artist,
            pm_market_id=outcome.pm_market_id,
            side=side,
            model_prob=p,
            prob_low=p,
            prob_high=p,
            market_price=yes_price,
            edge=raw_edge,
            confidence=0.0,
        ))

    edges.sort(key=lambda e: abs(e.edge), reverse=True)
    return edges
