"""Tests for intelligence.news_fetcher — parsing and forgiving error handling.

httpx is mocked so no network calls are made.
"""

import httpx
import pytest

from intelligence import news_fetcher
from intelligence.news_fetcher import NewsFetcher


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class _FakeClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, response=None, raise_exc=None):
        self._response = response
        self._raise_exc = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *args, **kwargs):
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def _patch_client(monkeypatch, response=None, raise_exc=None):
    monkeypatch.setattr(
        news_fetcher.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(response=response, raise_exc=raise_exc),
    )


@pytest.mark.asyncio
async def test_disabled_without_api_key():
    fetcher = NewsFetcher(api_key=None)
    assert await fetcher.fetch("anything") == []


@pytest.mark.asyncio
async def test_parses_articles(monkeypatch):
    payload = {
        "articles": [
            {
                "title": "Fed holds rates",
                "description": "The Fed left rates unchanged.",
                "source": {"name": "Reuters"},
                "publishedAt": "2026-06-17T12:00:00Z",
                "url": "https://example.com/a",
            },
            {
                "title": "Markets react",
                "description": None,
                "source": {"name": "BBC"},
                "publishedAt": "bad-date",
                "url": "https://example.com/b",
            },
        ]
    }
    _patch_client(monkeypatch, response=_FakeResponse(200, payload))
    fetcher = NewsFetcher(api_key="key")

    articles = await fetcher.fetch("Federal Reserve rates", max_articles=5)
    assert len(articles) == 2
    assert articles[0].title == "Fed holds rates"
    assert articles[0].source == "Reuters"
    assert articles[1].description is None  # tolerates missing description


@pytest.mark.asyncio
async def test_429_returns_empty_no_raise(monkeypatch):
    _patch_client(monkeypatch, response=_FakeResponse(429))
    fetcher = NewsFetcher(api_key="key")
    assert await fetcher.fetch("topic") == []


@pytest.mark.asyncio
async def test_401_disables_fetcher(monkeypatch):
    _patch_client(monkeypatch, response=_FakeResponse(401))
    fetcher = NewsFetcher(api_key="key")
    assert await fetcher.fetch("topic") == []
    assert fetcher._disabled is True  # disabled for the rest of the session


@pytest.mark.asyncio
async def test_timeout_returns_empty(monkeypatch):
    _patch_client(monkeypatch, raise_exc=httpx.TimeoutException("slow"))
    fetcher = NewsFetcher(api_key="key")
    assert await fetcher.fetch("topic") == []


@pytest.mark.asyncio
async def test_empty_results(monkeypatch):
    _patch_client(monkeypatch, response=_FakeResponse(200, {"articles": []}))
    fetcher = NewsFetcher(api_key="key")
    assert await fetcher.fetch("topic") == []
