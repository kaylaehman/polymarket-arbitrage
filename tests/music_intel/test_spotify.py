"""Tests for music_intel.sources.spotify — SpotifyClient (client-credentials flow)."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from music_intel.sources.spotify import SpotifyClient


def _resp(json_body, status=200):
    r = MagicMock()
    r.json = MagicMock(return_value=json_body)
    r.raise_for_status = MagicMock()
    r.status_code = status
    return r


def test_disabled_without_creds():
    c = SpotifyClient(http=MagicMock(), client_id=None, client_secret=None)
    assert c.enabled is False


@pytest.mark.asyncio
async def test_disabled_client_returns_none_empty():
    c = SpotifyClient(http=MagicMock(), client_id="", client_secret="")
    assert await c.search_artist("Drake") is None
    assert await c.release_momentum("x") == {}


@pytest.mark.asyncio
async def test_search_artist_parses_fields():
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp({"access_token": "tok", "expires_in": 3600}))
    http.get = AsyncMock(return_value=_resp({"artists": {"items": [
        {"id": "abc", "name": "Drake", "popularity": 100, "followers": {"total": 113000000}}
    ]}}))
    c = SpotifyClient(http=http, client_id="id", client_secret="sec")
    a = await c.search_artist("Drake")
    assert a == {"id": "abc", "name": "Drake", "popularity": 100, "followers": 113000000}
    # token cached: a second call does not re-POST the token
    http.get = AsyncMock(return_value=_resp({"artists": {"items": [
        {"id": "d2", "name": "X", "popularity": 1, "followers": {"total": 1}}
    ]}}))
    await c.search_artist("X")
    assert http.post.call_count == 1


@pytest.mark.asyncio
async def test_search_artist_no_match_returns_none():
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp({"access_token": "tok", "expires_in": 3600}))
    http.get = AsyncMock(return_value=_resp({"artists": {"items": []}}))
    c = SpotifyClient(http=http, client_id="id", client_secret="sec")
    assert await c.search_artist("Nobody") is None


@pytest.mark.asyncio
async def test_release_momentum_counts_year():
    http = MagicMock()
    http.post = AsyncMock(return_value=_resp({"access_token": "tok", "expires_in": 3600}))
    http.get = AsyncMock(return_value=_resp({"items": [
        {"album_type": "album", "release_date": "2026-05-15", "name": "A"},
        {"album_type": "album", "release_date": "2026-02-01", "name": "B"},
        {"album_type": "single", "release_date": "2026-03-03", "name": "C"},
        {"album_type": "album", "release_date": "2024-01-01", "name": "old"},
    ]}))
    c = SpotifyClient(http=http, client_id="id", client_secret="sec")
    m = await c.release_momentum("abc", year="2026")
    assert m["releases"] == 3 and m["albums"] == 2 and m["latest"] == "2026-05-15"


@pytest.mark.asyncio
async def test_errors_return_none_never_raise():
    http = MagicMock()
    http.post = AsyncMock(side_effect=RuntimeError("network down"))
    c = SpotifyClient(http=http, client_id="id", client_secret="sec")
    assert await c.search_artist("Drake") is None
    assert await c.release_momentum("abc") == {}
