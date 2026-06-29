"""Tests for music_intel.sources.ytd — 2026 YTD streams via kworb + Wayback Machine."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from music_intel.sources.ytd import _parse_totals, YtdSource

NOW = open("tests/music_intel/fixtures/kworb_artists_now.html").read()
JAN = open("tests/music_intel/fixtures/kworb_artists_jan.html").read()


def test_parse_totals_streams_column():
    t = _parse_totals(NOW)
    assert t["Bad Bunny"] == pytest.approx(124940.7)
    assert t["Drake"] == pytest.approx(136656.5)


def test_parse_empty_returns_empty():
    assert _parse_totals("") == {} and _parse_totals("<html>x</html>") == {}


def _http_for(now_html, jan_html, avail=True):
    http = MagicMock()

    async def _get(url, *a, **k):
        r = MagicMock()
        r.raise_for_status = MagicMock()
        if "archive.org/wayback/available" in url:
            r.json = MagicMock(
                return_value={
                    "archived_snapshots": {
                        "closest": {
                            "available": avail,
                            "url": "http://web.archive.org/web/20260102/https://kworb.net/spotify/artists.html",
                        }
                    }
                }
                if avail
                else {"archived_snapshots": {}}
            )
        elif "web.archive.org" in url:
            r.text = jan_html
        else:
            r.text = now_html
        return r

    http.get = AsyncMock(side_effect=_get)
    return http


@pytest.mark.asyncio
async def test_ytd_is_now_minus_jan():
    src = YtdSource(http=_http_for(NOW, JAN), baseline={})
    ytd = await src.ytd_2026()
    assert ytd["Bad Bunny"] == pytest.approx(124940.7 - 112089.0)  # 12851.7
    assert ytd["Drake"] == pytest.approx(136656.5 - 125679.0)  # 10977.5
    assert ytd["Bad Bunny"] > ytd["Drake"]  # BB leads 2026


@pytest.mark.asyncio
async def test_ytd_cached_second_call_no_refetch():
    http = _http_for(NOW, JAN)
    src = YtdSource(http=http, cache_ttl_s=9999, baseline={})
    await src.ytd_2026()
    n = http.get.call_count
    await src.ytd_2026()
    assert http.get.call_count == n  # served from cache


@pytest.mark.asyncio
async def test_no_wayback_snapshot_returns_empty():
    src = YtdSource(http=_http_for(NOW, JAN, avail=False), baseline={})
    assert await src.ytd_2026() == {}


@pytest.mark.asyncio
async def test_error_returns_empty():
    http = MagicMock()
    http.get = AsyncMock(side_effect=RuntimeError("down"))
    assert await YtdSource(http=http, baseline={}).ytd_2026() == {}


@pytest.mark.asyncio
async def test_wayback_availability_called_with_encoded_params():
    """Regression: archive.org returns empty archived_snapshots if the `url` is
    embedded with raw slashes; it must be passed via params= so httpx encodes it."""
    http = _http_for(NOW, JAN)
    await YtdSource(http=http, baseline={}).ytd_2026()
    avail = [c for c in http.get.call_args_list if "wayback/available" in c.args[0]]
    assert avail, "availability endpoint was not called"
    assert avail[0].kwargs.get("params", {}).get("url") == "kworb.net/spotify/artists.html"


@pytest.mark.asyncio
async def test_injected_baseline_skips_wayback():
    """With a baseline provided, only the current totals are fetched — no archive.org."""
    http = MagicMock()
    async def _get(url, *a, **k):
        r = MagicMock(); r.raise_for_status = MagicMock(); r.text = NOW; return r
    http.get = AsyncMock(side_effect=_get)
    src = YtdSource(http=http, baseline={"Bad Bunny": 112089.0, "Drake": 125679.0})
    ytd = await src.ytd_2026()
    assert ytd["Bad Bunny"] == pytest.approx(124940.7 - 112089.0)
    assert ytd["Drake"] == pytest.approx(136656.5 - 125679.0)
    # only the current-totals URL fetched; no wayback availability/snapshot calls
    assert all("archive.org" not in c.args[0] for c in http.get.call_args_list)


def test_baked_baseline_file_loads():
    """The committed Jan-1-2026 baseline file parses and has the key contenders."""
    from music_intel.sources.ytd import _load_baseline
    b = _load_baseline()
    assert b.get("Bad Bunny", 0) > 100000 and b.get("Drake", 0) > 100000
