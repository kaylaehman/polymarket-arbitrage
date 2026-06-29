"""
Tests for music_intel.sources.kworb_artists.

Uses fixture HTML — NO live network.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from music_intel.sources.kworb_artists import parse_artist_rates, KworbArtistSource

FIX = "tests/music_intel/fixtures/kworb_artists.html"


def test_parse_artist_rates_daily_column():
    html = open(FIX).read()
    rates = parse_artist_rates(html)
    assert rates["Drake"] == pytest.approx(57.662)
    assert rates["Bad Bunny"] == pytest.approx(51.226)
    assert rates["Taylor Swift"] == pytest.approx(40.372)
    assert len(rates) == 3


def test_parse_empty_or_garbage_returns_empty():
    assert parse_artist_rates("") == {}
    assert parse_artist_rates("<html>no table</html>") == {}


@pytest.mark.asyncio
async def test_source_fetch_uses_daily_url_and_parses():
    html = open(FIX).read()
    r = MagicMock(); r.text = html; r.raise_for_status = MagicMock()
    http = MagicMock(); http.get = AsyncMock(return_value=r)
    src = KworbArtistSource(http=http)
    rates = await src.fetch()
    assert rates["Drake"] == pytest.approx(57.662)
    # hit the artists.html URL
    assert "artists.html" in http.get.call_args.args[0]


@pytest.mark.asyncio
async def test_source_fetch_error_returns_empty():
    http = MagicMock(); http.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await KworbArtistSource(http=http).fetch() == {}
