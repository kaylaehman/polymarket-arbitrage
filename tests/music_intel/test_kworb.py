"""
Tests for music_intel.sources.kworb and music_intel.ratelimit.

Uses fixture HTML — NO live network.
"""
from __future__ import annotations

import asyncio
import pathlib
import time
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

# Guard: skip entire module cleanly if bs4 is not installed.
pytest.importorskip("bs4", reason="beautifulsoup4 not installed")

from music_intel.ratelimit import RateLimiter
from music_intel.sources.kworb import KworbSource, _parse_html

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
FIXTURE_HTML = (FIXTURE_DIR / "kworb_us_daily.html").read_text(encoding="utf-8")

_AS_OF = date(2026, 6, 27)


def _fake_http(status: int = 200, text: str = FIXTURE_HTML) -> MagicMock:
    """Build a minimal fake httpx.AsyncClient whose .get() returns a mock response."""
    response = MagicMock()
    response.status_code = status
    response.text = text

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    return client


# ---------------------------------------------------------------------------
# _parse_html unit tests (pure parser, no network)
# ---------------------------------------------------------------------------


class TestParseHtml:
    def test_returns_eight_records(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert len(records) == 8

    def test_record0_rank(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].rank == 1

    def test_record0_artist(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].artist == "Ella Langley"

    def test_record0_title(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].title == "Choosin' Texas"

    def test_record0_streams_period(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].streams_period == 1_726_927

    def test_record0_streams_7day(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].streams_7day == 11_427_563

    def test_record0_days_on_chart(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].days_on_chart == 254

    def test_record0_rank_delta_equal(self) -> None:
        """'=' in P+ column should produce rank_delta == 0."""
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].rank_delta == 0

    def test_record0_peak(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].peak == 1

    def test_record0_source_and_chart(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].source == "kworb"
        assert records[0].chart == "spotify_us_daily"

    def test_record0_as_of(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].as_of == _AS_OF

    def test_record0_track_id_none(self) -> None:
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[0].track_id is None

    def test_positive_rank_delta(self) -> None:
        """Row 3 (Drake, rank=3) has P+= '+2'."""
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[2].rank_delta == 2

    def test_negative_rank_delta(self) -> None:
        """Row 5 (Olivia Rodrigo, the cure, rank=5) has P+= '-2'."""
        records = _parse_html(FIXTURE_HTML, "spotify_us_daily", _AS_OF)
        assert records[4].rank_delta == -2

    def test_empty_html_returns_empty(self) -> None:
        records = _parse_html("<html><body></body></html>", "spotify_us_daily", _AS_OF)
        assert records == []


# ---------------------------------------------------------------------------
# KworbSource integration tests (fake HTTP client, no network)
# ---------------------------------------------------------------------------


class TestKworbSource:
    async def test_fetch_returns_eight_records(self) -> None:
        source = KworbSource(http=_fake_http())
        records = await source.fetch("spotify_us_daily", as_of=_AS_OF)
        assert len(records) == 8

    async def test_fetch_record0_matches_spec(self) -> None:
        source = KworbSource(http=_fake_http())
        records = await source.fetch("spotify_us_daily", as_of=_AS_OF)
        r = records[0]
        assert r.rank == 1
        assert r.artist == "Ella Langley"
        assert r.title == "Choosin' Texas"
        assert r.streams_period == 1_726_927
        assert r.streams_7day == 11_427_563
        assert r.days_on_chart == 254
        assert r.rank_delta == 0

    async def test_non_200_returns_empty(self) -> None:
        source = KworbSource(http=_fake_http(status=503))
        records = await source.fetch("spotify_us_daily", as_of=_AS_OF)
        assert records == []

    async def test_http_exception_returns_empty(self) -> None:
        """When the HTTP client raises, fetch must return [] — never raise."""
        client = MagicMock()
        client.get = AsyncMock(side_effect=ConnectionError("timeout"))
        source = KworbSource(http=client)
        records = await source.fetch("spotify_us_daily", as_of=_AS_OF)
        assert records == []

    async def test_unknown_chart_returns_empty(self) -> None:
        source = KworbSource(http=_fake_http())
        records = await source.fetch("unsupported_chart")
        assert records == []

    def test_name_property(self) -> None:
        source = KworbSource(http=_fake_http())
        assert source.name == "kworb"

    def test_trust_tier_property(self) -> None:
        source = KworbSource(http=_fake_http())
        assert source.trust_tier == 1

    async def test_as_of_defaults_to_today(self) -> None:
        source = KworbSource(http=_fake_http())
        records = await source.fetch("spotify_us_daily")
        assert records[0].as_of == date.today()


# ---------------------------------------------------------------------------
# RateLimiter tests (clock-mocked)
# ---------------------------------------------------------------------------


class TestRateLimiter:
    async def test_first_acquire_returns_true(self) -> None:
        limiter = RateLimiter(min_interval=0.0)
        result = await limiter.acquire("example.com")
        assert result is True

    async def test_daily_cap_returns_false(self) -> None:
        limiter = RateLimiter(min_interval=0.0, max_calls_per_day=1)
        await limiter.acquire("example.com")
        result = await limiter.acquire("example.com")
        assert result is False

    async def test_two_acquires_spaced_by_min_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two sequential acquires on the same host must be >= min_interval apart.

        We monkeypatch asyncio.sleep to record the sleep duration instead of
        actually sleeping, so the test runs instantly.
        """
        slept: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            slept.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        min_interval = 2.0
        limiter = RateLimiter(min_interval=min_interval, max_calls_per_day=100)

        # First acquire — no sleep needed (last=0, now > min_interval)
        await limiter.acquire("test.host")

        # Simulate a fast second call by back-dating last_call so wait > 0
        limiter._last_call["test.host"] = time.monotonic()  # set to now
        await limiter.acquire("test.host")

        # A sleep must have been requested with a positive duration close to
        # min_interval (exact value depends on how fast the CPU ran)
        assert len(slept) >= 1
        assert slept[-1] > 0
        assert slept[-1] <= min_interval + 0.1  # small tolerance

    async def test_different_hosts_independent(self) -> None:
        """Calls to different hosts should not block each other."""
        limiter = RateLimiter(min_interval=0.0, max_calls_per_day=10)
        r1 = await limiter.acquire("host-a.com")
        r2 = await limiter.acquire("host-b.com")
        assert r1 is True
        assert r2 is True

    async def test_daily_cap_resets_on_new_day(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a date roll-over between calls."""
        from datetime import datetime, timezone

        limiter = RateLimiter(min_interval=0.0, max_calls_per_day=1)
        await limiter.acquire("host.com")  # exhausts cap

        # Roll the date forward by patching datetime.now in the ratelimit module.
        future_dt = datetime(2099, 1, 2, 0, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "music_intel.ratelimit.datetime",
            type(
                "MockDatetime",
                (),
                {"now": staticmethod(lambda tz=None: future_dt)},
            ),
        )
        # After date rollover the cap should be reset
        result = await limiter.acquire("host.com")
        assert result is True
