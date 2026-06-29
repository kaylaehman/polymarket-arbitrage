"""
music_intel.sources.kworb — kworb.net chart data adapter.

Scrapes the kworb daily Spotify country charts using BeautifulSoup.
Never raises on parse or network errors — returns [] and logs a warning.

Supported chart slugs:
  "spotify_us_daily" → https://kworb.net/spotify/country/us_daily.html
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional

try:
    from bs4 import BeautifulSoup

    _BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BS4_AVAILABLE = False

from music_intel.ratelimit import RateLimiter
from music_intel.sources.base import ChartDataSource, ChartRecord

logger = logging.getLogger(__name__)

_USER_AGENT = "polymarket-arb-music-intel/1.0 (kaylaehman@pm.me)"

_CHART_URLS: dict[str, str] = {
    "spotify_us_daily": "https://kworb.net/spotify/country/us_daily.html",
}

_COMMAS = re.compile(r",")


def _parse_delta(raw: str) -> int:
    """Convert P+ cell text to integer delta.  '=' → 0, '+N'/'-N' → int."""
    raw = raw.strip()
    if raw == "=":
        return 0
    try:
        return int(_COMMAS.sub("", raw))
    except ValueError:
        return 0


def _parse_int(raw: str) -> int:
    """Strip commas and cast to int."""
    return int(_COMMAS.sub("", raw.strip()))


def _parse_artist_title(cell_text: str) -> tuple[str, str]:
    """Split 'Artist - Title' on the FIRST ' - '.  If not present, title=whole."""
    sep = " - "
    idx = cell_text.find(sep)
    if idx == -1:
        return "", cell_text.strip()
    return cell_text[:idx].strip(), cell_text[idx + len(sep):].strip()


def _parse_html(html: str, chart: str, as_of: date) -> list[ChartRecord]:
    """Parse kworb HTML table into ChartRecord list.  Never raises."""
    if not _BS4_AVAILABLE:  # pragma: no cover
        logger.error("[kworb] beautifulsoup4 not installed; cannot parse")
        return []

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    if tbody is None:
        logger.warning("[kworb] No <tbody> found in HTML for chart=%s", chart)
        return []

    records: list[ChartRecord] = []
    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        # Expect at least 9 cells: Pos P+ ArtistTitle Days Pk (x?) Streams Streams+ 7Day
        if len(cells) < 9:
            logger.debug("[kworb] Skipping short row (%d cells)", len(cells))
            continue
        try:
            rank = int(cells[0].get_text(strip=True))
            rank_delta = _parse_delta(cells[1].get_text(strip=True))
            # get_text() preserves the " - " text node between the two <a> tags
            artist_title_text = cells[2].get_text()
            artist, title = _parse_artist_title(artist_title_text)
            days_on_chart = _parse_int(cells[3].get_text(strip=True))
            peak = _parse_int(cells[4].get_text(strip=True))
            # cells[5] is the (x?) column — skip it
            streams_period = _parse_int(cells[6].get_text(strip=True))
            # cells[7] is Streams+ — skip
            streams_7day = _parse_int(cells[8].get_text(strip=True))
        except (ValueError, IndexError) as exc:
            logger.debug("[kworb] Skipping unparseable row: %s", exc)
            continue

        records.append(
            ChartRecord(
                source="kworb",
                chart=chart,
                as_of=as_of,
                rank=rank,
                title=title,
                artist=artist,
                track_id=None,
                rank_delta=rank_delta,
                streams_period=streams_period,
                streams_7day=streams_7day,
                days_on_chart=days_on_chart,
                peak=peak,
            )
        )

    return records


class KworbSource(ChartDataSource):
    """kworb.net chart data adapter (trust_tier=1, scraped)."""

    def __init__(
        self,
        http,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        """
        Args:
            http: An httpx.AsyncClient (or compatible mock).
            limiter: Optional shared RateLimiter; a default one is created if
                not provided.
        """
        self._http = http
        self._limiter = limiter or RateLimiter()

    @property
    def name(self) -> str:
        return "kworb"

    @property
    def trust_tier(self) -> int:
        return 1

    async def fetch(
        self,
        chart: str,
        as_of: Optional[date] = None,
    ) -> list[ChartRecord]:
        """Fetch and parse a kworb chart page.

        Returns an empty list on any error (network, HTTP, parse).
        """
        url = _CHART_URLS.get(chart)
        if url is None:
            logger.warning("[kworb] Unknown chart slug: %s", chart)
            return []

        from urllib.parse import urlparse

        host = urlparse(url).netloc
        allowed = await self._limiter.acquire(host)
        if not allowed:
            logger.warning("[kworb] Daily cap reached, skipping fetch for %s", chart)
            return []

        try:
            resp = await self._http.get(
                url, headers={"User-Agent": _USER_AGENT}
            )
            if resp.status_code != 200:
                logger.warning(
                    "[kworb] Non-200 response %d for %s", resp.status_code, url
                )
                return []
            html = resp.text
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kworb] HTTP error fetching %s: %s", url, exc)
            return []

        effective_date = as_of or date.today()
        return _parse_html(html, chart, effective_date)
