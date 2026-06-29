"""music_intel.artist_backtest — Wayback Machine replay of prior years.

Verifies whether the artist projection model retrodicts known Spotify Wrapped
global #1 winners by replaying historical kworb snapshots.

Strategy (the "Wayback delta" trick):
  - Jan snapshot: kworb artists.html as close to YYYY-01-01 as Wayback has.
  - As-of snapshot: kworb artists.html as close to YYYY-MM-01 as Wayback has.
  - YTD[artist] = asof_total - jan_total  (both are all-time cumulative totals).
  - daily_rate = YTD / days_elapsed  (honest average over the year-to-date;
    we have no historical "Daily" column, so the YTD average rate is the best proxy).
  - Project full-year winner via the existing project_top_artist model.

Never raises from public functions — returns None / {} on any data gap or error.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

_WAYBACK_AVAIL_URL = "http://archive.org/wayback/available"
_KWORB_URL = "kworb.net/spotify/artists.html"
_USER_AGENT = "music-intel/1.0 (+https://kaylas.systems)"


async def _wayback_html(http, timestamp: str) -> Optional[str]:
    """Discover the closest Wayback snapshot for kworb artists.html near YYYYMMDD.

    Uses params= so httpx encodes the URL value — raw slashes cause archive.org
    to return empty archived_snapshots.

    Returns the snapshot HTML text, or None on any gap or error.
    """
    try:
        avail = await http.get(
            _WAYBACK_AVAIL_URL,
            params={"url": _KWORB_URL, "timestamp": timestamp},
        )
        avail.raise_for_status()
        closest = avail.json().get("archived_snapshots", {}).get("closest")
        if not closest or not closest.get("url"):
            logger.debug("[backtest] No Wayback snapshot for %s", timestamp)
            return None
        snap = await http.get(closest["url"], headers={"User-Agent": _USER_AGENT})
        snap.raise_for_status()
        return snap.text or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[backtest] _wayback_html(%s) error: %s", timestamp, exc)
        return None


async def backtest_year(
    http,
    year: int,
    as_of_month: int = 6,
    top_n: int = 10,
) -> Optional[dict]:
    """Replay the projection model against historical Wayback snapshots.

    Fetches two snapshots:
      - YYYY-01-01: all-time totals at the start of the year (baseline).
      - YYYY-MM-01: all-time totals at the as-of date.

    YTD for each artist is the delta. The average YTD daily rate drives projection.

    Args:
        http: An httpx.AsyncClient (or compatible mock).
        year: Year to backtest (e.g. 2023, 2024).
        as_of_month: Month to treat as the "current" snapshot (default 6 = June).
        top_n: Number of top YTD artists to pass to the projection model.

    Returns:
        {
            "year": int,
            "as_of": str (YYYY-MM-01),
            "model_top": str,
            "ranking": [(name, prob), ...],
            "ytd_leader": str,
        }
        or None if either snapshot is missing / unparseable.
    """
    from music_intel.sources.ytd import _parse_totals
    from music_intel.artist_projection import project_top_artist

    jan_ts = f"{year}0101"
    asof_ts = f"{year}{as_of_month:02d}01"

    jan_html, asof_html = None, None
    try:
        jan_html = await _wayback_html(http, jan_ts)
        asof_html = await _wayback_html(http, asof_ts)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[backtest] snapshot fetch error for %d: %s", year, exc)
        return None

    if not jan_html or not asof_html:
        logger.info("[backtest] Missing snapshot(s) for %d — skipping", year)
        return None

    jan = _parse_totals(jan_html)
    asof = _parse_totals(asof_html)

    if not jan or not asof:
        logger.info("[backtest] Empty parse for %d — skipping", year)
        return None

    asof_date = date(year, as_of_month, 1)
    jan_date = date(year, 1, 1)
    days_elapsed = max((asof_date - jan_date).days, 1)
    days_remaining = max(365 - days_elapsed, 0)

    ytd: dict[str, float] = {
        a: asof[a] - jan[a]
        for a in asof
        if a in jan and asof[a] - jan[a] > 0
    }
    if not ytd:
        logger.info("[backtest] No positive YTD artists for %d", year)
        return None

    top_artists = sorted(ytd, key=lambda a: ytd[a], reverse=True)[:top_n]
    ytd_leader = top_artists[0]

    contenders = [
        {
            "name": a,
            "daily_rate": ytd[a] / days_elapsed,
            "ytd_estimate": ytd[a],
            "albums_2026": 0,
            "days_since_release": None,
        }
        for a in top_artists
    ]

    projections = project_top_artist(contenders, days_remaining, days_elapsed)
    if not projections:
        return None

    ranking = [(p.name, p.prob) for p in projections]
    model_top = projections[0].name

    return {
        "year": year,
        "as_of": f"{year}-{as_of_month:02d}-01",
        "model_top": model_top,
        "ranking": ranking,
        "ytd_leader": ytd_leader,
    }


def score_backtest(result: dict, actual_winner: str) -> dict:
    """Score a backtest result against the known actual winner.

    Args:
        result: Dict returned by backtest_year.
        actual_winner: The actual Spotify Wrapped global #1 artist for that year.

    Returns:
        {
            "year": int,
            "model_top": str,
            "actual_winner": str,
            "correct": bool,
            "winner_rank": int | None  (1-based; None if actual_winner not in ranking),
        }
    """
    ranking_names = [name for name, _ in result.get("ranking", [])]
    winner_rank: Optional[int] = None
    for idx, name in enumerate(ranking_names, start=1):
        if name == actual_winner:
            winner_rank = idx
            break

    return {
        "year": result.get("year"),
        "model_top": result.get("model_top"),
        "actual_winner": actual_winner,
        "correct": result.get("model_top") == actual_winner,
        "winner_rank": winner_rank,
    }
