"""
music_intel.sources.kworb_artists — kworb.net global artist streaming rates.

Scrapes https://kworb.net/spotify/artists.html and returns each artist's
current daily streaming rate in millions.

Never raises on parse or network errors — returns {} on any failure.
"""
from __future__ import annotations

import logging

try:
    from bs4 import BeautifulSoup

    _BS4_AVAILABLE = True
except ImportError:  # pragma: no cover
    _BS4_AVAILABLE = False

logger = logging.getLogger(__name__)

_USER_AGENT = "polymarket-arb-music-intel/1.0 (kaylaehman@pm.me)"
_ARTISTS_URL = "https://kworb.net/spotify/artists.html"

# Column index of the "Daily" streaming rate within each data row.
_DAILY_COL = 2


def parse_artist_rates(html: str) -> dict[str, float]:
    """Parse kworb artists page HTML into {artist_name: daily_streams_millions}.

    Returns {} on empty input, missing table, or any parse error.
    """
    if not html or not _BS4_AVAILABLE:
        return {}

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return {}

        rates: dict[str, float] = {}
        for row in table.find_all("tr"):
            # Skip header rows (contain <th> elements)
            if row.find("th"):
                continue
            cells = row.find_all("td")
            if len(cells) <= _DAILY_COL:
                continue
            try:
                artist_cell = cells[0]
                artist = artist_cell.get_text(strip=True)
                daily_raw = cells[_DAILY_COL].get_text(strip=True).replace(",", "")
                rates[artist] = float(daily_raw)
            except (ValueError, AttributeError) as exc:
                logger.debug("[kworb_artists] Skipping unparseable row: %s", exc)
                continue

        return rates
    except Exception as exc:  # noqa: BLE001
        logger.warning("[kworb_artists] Parse error: %s", exc)
        return {}


class KworbArtistSource:
    """Fetches and parses the kworb global artist daily-streaming-rate table."""

    def __init__(self, http) -> None:
        """
        Args:
            http: An httpx.AsyncClient (or compatible mock).
        """
        self._http = http

    async def fetch(self, url: str = _ARTISTS_URL) -> dict[str, float]:
        """Fetch artist daily rates from kworb.

        Returns {} on any network or parse error.
        """
        try:
            resp = await self._http.get(
                url, headers={"User-Agent": _USER_AGENT}
            )
            resp.raise_for_status()
            return parse_artist_rates(resp.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[kworb_artists] Fetch error from %s: %s", url, exc)
            return {}
