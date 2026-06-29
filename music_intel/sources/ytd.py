"""
music_intel.sources.ytd — 2026 YTD Spotify stream totals via kworb + Wayback Machine.

YTD-2026 = current_all_time_total - jan_1_2026_all_time_total

Never raises — returns {} on any error (graceful degrade).
"""
from __future__ import annotations

import json
import logging
import os
import time

try:
    from bs4 import BeautifulSoup

    _BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BS4_AVAILABLE = False

logger = logging.getLogger(__name__)

_ARTISTS_URL = "https://kworb.net/spotify/artists.html"
# NOTE: pass the query as params= so httpx URL-encodes the `url` value. Embedding
# it with raw slashes (?url=kworb.net/spotify/artists.html) makes archive.org
# return an empty archived_snapshots — the encoding matters.
_WAYBACK_AVAIL_URL = "http://archive.org/wayback/available"
_WAYBACK_PARAMS = {"url": "kworb.net/spotify/artists.html", "timestamp": "20260101"}
_USER_AGENT = "music-intel/1.0 (+https://kaylas.systems)"

# Static Jan-1-2026 baseline (kworb all-time totals), baked once from the Wayback
# snapshot so runtime needs only the live current totals — no slow/flaky archive.org
# fetch on the hot path. Wayback is used only as a fallback if the file is missing.
_BASELINE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "kworb_jan2026_totals.json")


def _load_baseline() -> dict[str, float]:
    """Load the baked Jan-1-2026 totals from the local data file. {} if absent."""
    try:
        with open(_BASELINE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        totals = data.get("totals", data) if isinstance(data, dict) else {}
        return {k: float(v) for k, v in totals.items()}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ytd] baseline file unavailable: %s", exc)
        return {}


def _is_artist_anchor(href: str | None) -> bool:
    """True for both the current kworb format (/spotify/artist/<id>_songs.html)
    and the old relative format (artist/<id>.html or <id>.html inside an artist dir).

    Matches any href containing 'artist/' — covers:
      - Current: /spotify/artist/3TVXtAsR1Inumwj472S9r4_songs.html
      - Old/relative: artist/3TVXtAsR1Inumwj472S9r4.html
      - Wayback-wrapped: /web/20230101/https://kworb.net/spotify/artist/...
    """
    return bool(href and "artist/" in href)


def _find_artist_cell(cells):
    """Return (index, artist_name) for the first cell containing an artist anchor.

    Returns (None, None) if no such anchor exists in any cell.
    """
    for i, cell in enumerate(cells):
        anchor = cell.find("a", href=_is_artist_anchor)
        if anchor is not None:
            return i, anchor.get_text(strip=True)
    return None, None


def _parse_streams_after(cells, artist_idx: int) -> float | None:
    """Return the first float-parseable value in cells after artist_idx."""
    for cell in cells[artist_idx + 1 :]:
        raw = cell.get_text(strip=True).replace(",", "")
        try:
            return float(raw)
        except ValueError:
            continue
    return None


def _parse_totals(html: str) -> dict[str, float]:
    """Parse kworb artists HTML into {artist: all_time_total_millions}.

    Layout-agnostic: finds the artist cell by its /spotify/artist/ anchor href,
    then takes the first float-parseable cell that follows it as the streams total.
    Handles both the current format (artist=col0) and old snapshots (rank=col0,
    artist=col1) without hard-coding column indices.

    Returns {} on empty input, missing table, or any parse error.
    """
    if not html or not _BS4_AVAILABLE:
        return {}

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return {}

        totals: dict[str, float] = {}
        for row in table.find_all("tr"):
            if row.find("th"):
                continue
            cells = row.find_all("td")
            artist_idx, artist = _find_artist_cell(cells)
            if artist_idx is None or not artist:
                logger.debug("[ytd] Skipping row with no /spotify/artist/ anchor")
                continue
            streams = _parse_streams_after(cells, artist_idx)
            if streams is None:
                logger.debug("[ytd] Skipping row with no parseable streams value: %s", artist)
                continue
            totals[artist] = streams

        return totals
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ytd] Parse error: %s", exc)
        return {}


class YtdSource:
    """Computes 2026 YTD Spotify stream totals (millions) per artist.

    Strategy:
      1. Fetch current all-time totals from kworb.net/spotify/artists.html.
      2. Discover the nearest Jan-1-2026 Wayback Machine snapshot URL.
      3. Fetch that snapshot and parse its all-time totals.
      4. YTD = current - jan1 for artists present in both.

    Results are cached for `cache_ttl_s` seconds (default 6 hours).
    Returns {} on any error.
    """

    def __init__(self, http, cache_ttl_s: int = 21600, baseline: dict | None = None) -> None:
        """
        Args:
            http: An httpx.AsyncClient (or compatible mock).
            cache_ttl_s: How long to cache the computed YTD dict (seconds).
            baseline: Optional Jan-1 totals override (for tests). When None, the
                baked local file is used, falling back to a live Wayback fetch.
        """
        self._http = http
        self._cache_ttl_s = cache_ttl_s
        self._baseline = baseline
        self._cache: dict[str, float] | None = None
        self._cache_ts: float = 0.0

    async def ytd_2026(self) -> dict[str, float]:
        """Return {artist: ytd_streams_millions} for 2026, from cache or live."""
        if self._cache is not None and (
            time.monotonic() - self._cache_ts < self._cache_ttl_s
        ):
            return self._cache

        try:
            result = await self._fetch_ytd()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[ytd] Unexpected error computing YTD: %s", exc)
            result = {}

        if result:
            self._cache = result
            self._cache_ts = time.monotonic()

        return result

    async def _fetch_ytd(self) -> dict[str, float]:
        """Core fetch logic — may raise; caller wraps in try/except."""
        # Step 1: current totals (live — fast, reliable).
        resp_now = await self._http.get(
            _ARTISTS_URL, headers={"User-Agent": _USER_AGENT}
        )
        resp_now.raise_for_status()
        now = _parse_totals(resp_now.text)
        if not now:
            logger.warning("[ytd] Failed to parse current kworb totals")
            return {}

        # Step 2: Jan-1 baseline — injected override, else local baked file (no
        # archive.org on the hot path), else a live Wayback fetch as last resort.
        jan = self._baseline if self._baseline is not None else _load_baseline()
        if not jan:
            jan = await self._fetch_wayback_baseline()
        if not jan:
            return {}

        # Step 3: YTD for artists present in both.
        return {artist: now[artist] - jan[artist] for artist in now if artist in jan}

    async def _fetch_wayback_baseline(self) -> dict[str, float]:
        """Fallback: fetch the Jan-1-2026 totals live from the Wayback Machine."""
        resp_avail = await self._http.get(_WAYBACK_AVAIL_URL, params=_WAYBACK_PARAMS)
        resp_avail.raise_for_status()
        closest = resp_avail.json().get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("url"):
            logger.warning("[ytd] No Wayback snapshot found and no baked baseline")
            return {}
        resp_jan = await self._http.get(closest["url"], headers={"User-Agent": _USER_AGENT})
        resp_jan.raise_for_status()
        return _parse_totals(resp_jan.text)
