"""
music_intel.sources.ytd — 2026 YTD Spotify stream totals via kworb + Wayback Machine.

YTD-2026 = current_all_time_total - jan_1_2026_all_time_total

Never raises — returns {} on any error (graceful degrade).
"""
from __future__ import annotations

import logging
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

# Column index 1 = "Streams" (all-time cumulative total in millions).
_STREAMS_COL = 1


def _parse_totals(html: str) -> dict[str, float]:
    """Parse kworb artists HTML into {artist: all_time_total_millions}.

    Returns {} on empty input, missing table, or any parse error.
    Column index 1 ("Streams") is the all-time cumulative total.
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
            if len(cells) <= _STREAMS_COL:
                continue
            try:
                artist = cells[0].get_text(strip=True)
                raw = cells[_STREAMS_COL].get_text(strip=True).replace(",", "")
                totals[artist] = float(raw)
            except (ValueError, AttributeError) as exc:
                logger.debug("[ytd] Skipping unparseable row: %s", exc)
                continue

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

    def __init__(self, http, cache_ttl_s: int = 21600) -> None:
        """
        Args:
            http: An httpx.AsyncClient (or compatible mock).
            cache_ttl_s: How long to cache the computed YTD dict (seconds).
        """
        self._http = http
        self._cache_ttl_s = cache_ttl_s
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
        # Step 1: current totals.
        resp_now = await self._http.get(
            _ARTISTS_URL, headers={"User-Agent": _USER_AGENT}
        )
        resp_now.raise_for_status()
        now = _parse_totals(resp_now.text)
        if not now:
            logger.warning("[ytd] Failed to parse current kworb totals")
            return {}

        # Step 2: discover Wayback snapshot URL for Jan 1 2026.
        resp_avail = await self._http.get(_WAYBACK_AVAIL_URL, params=_WAYBACK_PARAMS)
        resp_avail.raise_for_status()
        avail_json = resp_avail.json()
        closest = avail_json.get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("url"):
            logger.warning("[ytd] No Wayback snapshot found for Jan 1 2026")
            return {}

        jan_url = closest["url"]

        # Step 3: fetch Jan snapshot.
        resp_jan = await self._http.get(jan_url, headers={"User-Agent": _USER_AGENT})
        resp_jan.raise_for_status()
        jan = _parse_totals(resp_jan.text)
        if not jan:
            logger.warning("[ytd] Failed to parse Jan snapshot totals")
            return {}

        # Step 4: compute YTD for artists present in both snapshots.
        return {artist: now[artist] - jan[artist] for artist in now if artist in jan}
