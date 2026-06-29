import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from music_intel.sources.markets import (
    is_music_market, discover_polymarket, discover_kalshi, MarketCandidate,
    parse_market_target,
)

FIXTURE = "tests/music_intel/fixtures/gamma_markets.json"


def _gamma_resp():
    data = json.load(open(FIXTURE))
    r = MagicMock(); r.json = MagicMock(return_value=data); r.raise_for_status = MagicMock()
    return r


def test_is_music_market():
    assert is_music_market("Will X be the Billboard Hot 100 #1?") is True
    assert is_music_market("Will Y be #1 on Spotify this week?") is True
    assert is_music_market("Blockx vs. Zverev: Match O/U 36.5") is False


def test_is_music_market_excludes_lookalikes():
    # A stock market whose description mentions a price "chart" / all-time-high
    # must NOT be treated as music (bare "#1"/"chart" used to leak these in).
    assert is_music_market("Will Palantir (PLTR) hit (HIGH) $114 in July?",
                           "Resolves YES if the price chart prints a new #1 high.") is False
    # A movie market ("top ... movie") is not music either.
    assert is_music_market("Will 'Maternal Instinct' be the top global Netflix movie this week?") is False
    # No music anchor (billboard/spotify/etc.) + no song/album noun -> not music.
    assert is_music_market("Will Team A be the #1 seed in the playoffs?") is False


def test_parse_market_target_extracts_artist_and_title():
    # Markets name a SPECIFIC track as "Title - Artist" in quotes.
    a, t = parse_market_target('Will "Drop Dead - Olivia Rodrigo" be the Billboard Hot 100 #1 song?')
    assert (a, t) == ("Olivia Rodrigo", "Drop Dead")
    a, t = parse_market_target('Will "The Wow! Signal - Muse" be the Billboard 200 #1 album?')
    assert (a, t) == ("Muse", "The Wow! Signal")  # title may contain punctuation/spaces


def test_parse_market_target_artist_level_question_returns_empty():
    # No quoted "Title - Artist" -> no specific track parsed.
    assert parse_market_target("Will Taylor Swift be #1 on the Hot 100?") == ("", "")


@pytest.mark.asyncio
async def test_discover_polymarket_filters_music_only():
    http = MagicMock(); http.get = AsyncMock(return_value=_gamma_resp())
    cands = await discover_polymarket(http)
    assert len(cands) >= 1
    assert all(isinstance(c, MarketCandidate) and c.venue == "polymarket" for c in cands)
    assert all(is_music_market(c.question) for c in cands)
    # prices parsed from JSON-string to floats
    for c in cands:
        assert all(isinstance(p, float) for p in c.prices)
        assert c.question  # real question present


@pytest.mark.asyncio
async def test_discover_polymarket_error_returns_empty():
    http = MagicMock(); http.get = AsyncMock(side_effect=RuntimeError("gamma down"))
    assert await discover_polymarket(http) == []


@pytest.mark.asyncio
async def test_discover_kalshi_filters_and_normalizes():
    m = SimpleNamespace(ticker="KXHOT100-26-X", title="Will X be #1 on the Billboard Hot 100?",
                        subtitle="resolves on Billboard", yes_price=0.07, volume=500,
                        close_time=None)
    other = SimpleNamespace(ticker="KXHIGHNY-26JUN29-B83.5", title="NYC high temp",
                            subtitle="", yes_price=0.9, volume=10, close_time=None)
    kc = MagicMock(); kc.list_markets = AsyncMock(return_value=([m, other], None))
    cands = await discover_kalshi(kc)
    assert len(cands) == 1
    c = cands[0]
    assert c.venue == "kalshi" and c.market_id == "kalshi:KXHOT100-26-X"
    assert c.prices == [0.07, 0.93]


@pytest.mark.asyncio
async def test_discover_kalshi_none_client_empty():
    assert await discover_kalshi(None) == []
