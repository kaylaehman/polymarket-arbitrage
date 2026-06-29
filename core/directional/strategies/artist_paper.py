"""ArtistPaperStrategy — projects P(#1 Spotify Artist) and emits PAPER candidates.

Orchestrates the full artist pipeline:
  1. Discover the Top-Spotify-Artist Polymarket market via Gamma API.
  2. Gather each contender's YTD streams, daily rate, and optional Spotify
     release activity.
  3. Project P(#1) with confidence bands via artist_projection.
  4. Detect band-based edges via artist_markets.compute_artist_edges.
  5. Emit DirectionalCandidate objects (category="music"). NEVER trades.

Settlement reuses the existing pm: tracker path.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from core.directional.models import DirectionalCandidate
from core.directional.strategies.base import Strategy

logger = logging.getLogger(__name__)


class ArtistPaperStrategy(Strategy):
    """Paper-only strategy for the Top-Spotify-Artist Polymarket market.

    All dependencies (data sources, Spotify client) are injectable for
    testability. Network is never touched in tests.
    """

    def __init__(
        self,
        *,
        http: Any,
        spotify_client: Any = None,
        year: str = "2026",
        min_edge: float = 0.10,
        max_contenders: int = 12,
        min_refresh_seconds: float = 86400.0,
        ytd_source: Any = None,
        rate_source: Any = None,
        today: date | None = None,
        now_fn=time.monotonic,
    ) -> None:
        # Lazy-import music_intel pieces so importing this module never hard-fails.
        from music_intel.sources.ytd import YtdSource
        from music_intel.sources.kworb_artists import KworbArtistSource

        self._http = http
        self._spotify = spotify_client
        self._year = year
        self._min_edge = min_edge
        self._max_contenders = max_contenders
        self._min_refresh = min_refresh_seconds
        self._ytd_source = ytd_source if ytd_source is not None else YtdSource(http)
        self._rate_source = rate_source if rate_source is not None else KworbArtistSource(http)
        self._today = today  # None -> use date.today() at scan time
        self._now_fn = now_fn
        self._last_run: float | None = None

    @property
    def name(self) -> str:
        return "artist_paper"

    async def scan(self, markets: list, ctx: dict[str, Any]) -> list[DirectionalCandidate]:
        """Discover artist markets, project P(#1), and return paper candidates."""
        try:
            return await self._scan_inner()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[artist_paper] scan() error (returning []): %s", exc)
            return []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _scan_inner(self) -> list[DirectionalCandidate]:
        # Step 1: throttle
        now = self._now_fn()
        if self._last_run is not None and (now - self._last_run) < self._min_refresh:
            return []
        self._last_run = now

        # Lazy-import at scan time so shifting music_intel deps never break import.
        from music_intel.artist_markets import discover_top_artist_markets, compute_artist_edges
        from music_intel.artist_projection import project_top_artist

        # Step 2: discover markets
        outcomes = await discover_top_artist_markets(self._http, self._year)
        if not outcomes:
            logger.info("[artist_paper] no Top-Spotify-Artist market discovered for %s", self._year)
            return []

        # Step 3: gather stream data
        ytd = await self._ytd_source.ytd_2026()
        rates = await self._rate_source.fetch()

        # Step 4: pick top-N outcomes by yes_price (market-implied favourites)
        top_outcomes = sorted(outcomes, key=lambda o: o.yes_price, reverse=True)[: self._max_contenders]

        # Step 5: build contender dicts (with optional Spotify enrichment)
        today = self._today if self._today is not None else date.today()
        contenders = []
        for o in top_outcomes:
            contender = await self._build_contender(o, ytd, rates, today)
            contenders.append(contender)

        # Step 6: compute days elapsed / remaining
        year_int = int(self._year)
        start = date(year_int, 1, 1)
        end = date(year_int, 12, 31)
        days_elapsed = (today - start).days
        days_remaining = max(0, (end - today).days)

        # Step 7: project
        projs = project_top_artist(
            contenders,
            days_remaining=days_remaining,
            days_elapsed=days_elapsed,
        )

        # Step 8: detect edges
        edges = compute_artist_edges(projs, outcomes, min_edge=self._min_edge)

        # Visibility: one line per run so we can see the funnel (and that it ran).
        logger.info(
            "[artist_paper] funnel: %d outcomes, ytd=%d rates=%d contenders=%d -> %d edge(s): %s",
            len(outcomes), len(ytd), len(rates), len(contenders), len(edges),
            ", ".join(f"{e.side} {e.artist} {e.edge:+.2f}" for e in edges) or "none",
        )

        # Step 9: build candidates
        return [self._make_candidate(edge) for edge in edges]

    async def _build_contender(self, outcome: Any, ytd: dict, rates: dict, today: date) -> dict:
        """Return a contender dict for project_top_artist."""
        daily_rate = rates.get(outcome.artist, 0.0)
        ytd_estimate = ytd.get(outcome.artist)  # may be None
        albums_2026 = 0
        days_since_release = None

        if self._spotify and getattr(self._spotify, "enabled", False):
            artist_info = await self._spotify.search_artist(outcome.artist)
            if artist_info:
                rm = await self._spotify.release_momentum(artist_info["id"], self._year)
                albums_2026 = rm.get("albums", 0)
                latest = rm.get("latest")
                if latest:
                    try:
                        latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
                        days_since_release = (today - latest_date).days
                    except (ValueError, TypeError):
                        pass

        return {
            "name": outcome.artist,
            "daily_rate": daily_rate,
            "albums_2026": albums_2026,
            "days_since_release": days_since_release,
            "ytd_estimate": ytd_estimate,
        }

    def _make_candidate(self, edge: Any) -> DirectionalCandidate:
        """Convert an ArtistEdge into a DirectionalCandidate."""
        if edge.side == "YES":
            market_price = edge.market_price
        else:
            market_price = round(1 - edge.market_price, 4)

        return DirectionalCandidate(
            market_id=edge.pm_market_id,
            title=f"Top Spotify Artist {self._year}: {edge.artist}",
            category="music",
            side=edge.side,
            market_price=market_price,
            ai_probability=edge.model_prob,
            confidence=None,
            edge=abs(edge.edge),
            strategy=self.name,
            reasoning=(
                f"model P(#1)={edge.model_prob:.2f} vs market {edge.market_price:.2f}"
                f" -> {edge.side} {edge.artist}"
            ),
        )
