"""Tests for ArtistPaperStrategy — pure paper bets on Top-Spotify-Artist markets.

All network calls are faked via injected sources. No real I/O.
asyncio_mode=auto (pytest.ini) means no @pytest.mark.asyncio needed.
"""
import json
import datetime
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.directional.strategies.artist_paper import ArtistPaperStrategy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gamma_http(markets):
    """Build a fake httpx-like client that returns a Gamma events payload."""
    evt = [{"title": "Top Spotify Artist 2026", "markets": [
        {
            "id": str(i),
            "groupItemTitle": n,
            "outcomePrices": json.dumps([str(p), str(round(1 - p, 3))]),
        }
        for i, (n, p) in enumerate(markets)
    ]}]
    r = MagicMock()
    r.json = MagicMock(return_value=evt)
    r.raise_for_status = MagicMock()
    h = MagicMock()
    h.get = AsyncMock(return_value=r)
    return h


class _YTD:
    def __init__(self, d):
        self._d = d

    async def ytd_2026(self):
        return self._d


class _Rates:
    def __init__(self, d):
        self._d = d

    async def fetch(self):
        return self._d


def _strat(http, ytd, rates, spotify=None, **kw):
    defaults = dict(
        min_refresh_seconds=0,
        min_edge=0.10,
    )
    defaults.update(kw)
    return ArtistPaperStrategy(
        http=http,
        spotify_client=spotify,
        ytd_source=_YTD(ytd),
        rate_source=_Rates(rates),
        today=datetime.date(2026, 6, 29),
        **defaults,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

async def test_emits_bad_bunny_no_overpriced():
    """Bad Bunny at 83.5c YES is overpriced vs model -> expect NO candidate."""
    http = _gamma_http([
        ("Bad Bunny", 0.835),
        ("Drake", 0.081),
        ("Taylor Swift", 0.035),
    ])
    ytd = {"Bad Bunny": 12852, "Drake": 10978, "Taylor Swift": 8900}
    rates = {"Bad Bunny": 51.2, "Drake": 57.7, "Taylor Swift": 40.4}

    cands = await _strat(http, ytd, rates).scan([], {})

    bb = [c for c in cands if c.title.endswith("Bad Bunny")]
    assert bb, "Expected at least one Bad Bunny candidate"
    assert bb[0].side == "NO"
    assert bb[0].category == "music"
    assert bb[0].market_id == "pm:0"
    assert bb[0].strategy == "artist_paper"
    # NO entry price = 1 - yes_price = 1 - 0.835
    assert bb[0].market_price == pytest.approx(0.165)


async def test_no_market_returns_empty():
    """When the Gamma API returns no events, scan() must return []."""
    h = MagicMock()
    h.get = AsyncMock(return_value=MagicMock(
        json=MagicMock(return_value=[]),
        raise_for_status=MagicMock(),
    ))
    cands = await ArtistPaperStrategy(
        http=h,
        ytd_source=_YTD({}),
        rate_source=_Rates({}),
        today=datetime.date(2026, 6, 29),
        min_refresh_seconds=0,
    ).scan([], {})
    assert cands == []


async def test_throttle_skips_second_call():
    """Second scan() within min_refresh_seconds window must return []."""
    http = _gamma_http([("Bad Bunny", 0.835), ("Drake", 0.081)])
    s = _strat(
        http,
        {"Bad Bunny": 12852, "Drake": 10978},
        {"Bad Bunny": 51.2, "Drake": 57.7},
        min_refresh_seconds=9999,
    )
    first = await s.scan([], {})
    second = await s.scan([], {})
    assert len(first) >= 1
    assert second == []


async def test_scan_swallows_errors():
    """Any exception inside scan() must be caught and return []."""
    h = MagicMock()
    h.get = AsyncMock(side_effect=RuntimeError("boom"))
    s = ArtistPaperStrategy(
        http=h,
        ytd_source=_YTD({}),
        rate_source=_Rates({}),
        today=datetime.date(2026, 6, 29),
        min_refresh_seconds=0,
    )
    assert await s.scan([], {}) == []


async def test_works_without_spotify():
    """With spotify=None, albums_2026=0 and days_since_release=None; still projects + edges."""
    http = _gamma_http([("Bad Bunny", 0.835), ("Drake", 0.081)])
    cands = await _strat(
        http,
        {"Bad Bunny": 12852, "Drake": 10978},
        {"Bad Bunny": 51.2, "Drake": 57.7},
        spotify=None,
    ).scan([], {})
    assert any(c.title.endswith("Bad Bunny") and c.side == "NO" for c in cands)
