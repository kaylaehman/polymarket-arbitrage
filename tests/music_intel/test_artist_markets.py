"""Tests for music_intel.artist_markets — Top-Spotify-Artist market discovery + edge detection."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from music_intel.artist_markets import (
    _normalize,
    discover_top_artist_markets,
    compute_artist_edges,
    ArtistOutcome,
    ArtistEdge,
)
from music_intel.artist_projection import ArtistProjection


def _event(markets):
    return [{"title": "Top Spotify Artist 2026", "markets": [
        {"id": str(i), "groupItemTitle": n, "outcomePrices": json.dumps([str(p), str(round(1 - p, 3))])}
        for i, (n, p) in enumerate(markets)
    ]}]


def _http(evt):
    r = MagicMock()
    r.json = MagicMock(return_value=evt)
    r.raise_for_status = MagicMock()
    h = MagicMock()
    h.get = AsyncMock(return_value=r)
    return h


def test_normalize_strips_accents_and_case():
    assert _normalize("Beyoncé") == _normalize("beyonce")
    assert _normalize("Bad  Bunny") == _normalize("bad bunny")


@pytest.mark.asyncio
async def test_discover_parses_outcomes_with_pm_prefix():
    outs = await discover_top_artist_markets(_http(_event([("Bad Bunny", 0.835), ("Drake", 0.081)])))
    assert len(outs) == 2
    bb = [o for o in outs if o.artist == "Bad Bunny"][0]
    assert bb.pm_market_id.startswith("pm:") and bb.yes_price == pytest.approx(0.835)


@pytest.mark.asyncio
async def test_discover_error_returns_empty():
    h = MagicMock()
    h.get = AsyncMock(side_effect=RuntimeError("gamma down"))
    assert await discover_top_artist_markets(h) == []


def _proj(name, prob, lo, hi):
    return ArtistProjection(
        name=name, projected_units=0.0, prob=prob, prob_low=lo, prob_high=hi,
        confidence=0.5, drivers=[],
    )


def test_overpriced_triggers_NO():
    # Bad Bunny: model 0.46 band [0.24, 0.68], market 0.83 -> NO (overpriced)
    outs = [ArtistOutcome("Bad Bunny", "pm:0", 0.83)]
    edges = compute_artist_edges([_proj("Bad Bunny", 0.46, 0.24, 0.68)], outs)
    assert len(edges) == 1 and edges[0].side == "NO"
    assert edges[0].edge == pytest.approx(0.46 - 0.83)


def test_underpriced_triggers_YES():
    outs = [ArtistOutcome("The Weeknd", "pm:1", 0.01)]
    edges = compute_artist_edges([_proj("The Weeknd", 0.03, 0.02, 0.05)], outs)
    assert len(edges) == 1 and edges[0].side == "YES"


def test_inside_band_no_edge():
    # Drake: market 0.08 inside band [0.05, 0.71] -> no edge despite model 0.38
    outs = [ArtistOutcome("Drake", "pm:2", 0.08)]
    assert compute_artist_edges([_proj("Drake", 0.38, 0.05, 0.71)], outs) == []


def test_name_match_is_accent_insensitive():
    outs = [ArtistOutcome("Beyoncé", "pm:3", 0.50)]
    edges = compute_artist_edges([_proj("Beyonce", 0.10, 0.05, 0.20)], outs)  # market 0.50 > hi 0.20 -> NO
    assert len(edges) == 1 and edges[0].side == "NO" and edges[0].artist == "Beyoncé"


def test_min_edge_filters_small():
    outs = [ArtistOutcome("X", "pm:9", 0.06)]
    # model 0.03 band [0.02, 0.04]; market 0.06 > hi 0.04 -> NO, but |0.03-0.06|=0.03 < min_edge 0.05 -> filtered
    assert compute_artist_edges([_proj("X", 0.03, 0.02, 0.04)], outs, min_edge=0.05) == []


@pytest.mark.asyncio
async def test_discover_artist_market_by_slug():
    from music_intel.artist_markets import discover_artist_market
    import json
    evt=[{"title":"X","markets":[{"id":"7","groupItemTitle":"Bad Bunny","outcomePrices":json.dumps(["0.5","0.5"])}]}]
    from unittest.mock import AsyncMock, MagicMock
    r=MagicMock(); r.json=MagicMock(return_value=evt); r.raise_for_status=MagicMock()
    http=MagicMock(); http.get=AsyncMock(return_value=r)
    outs=await discover_artist_market(http, "top-spotify-artist-in-june")
    assert len(outs)==1 and outs[0].pm_market_id=="pm:7"
    # the slug was passed through to the gamma query
    assert any("top-spotify-artist-in-june" in str(c.kwargs.get("params","")) or "top-spotify-artist-in-june" in str(c.args) for c in http.get.call_args_list)
