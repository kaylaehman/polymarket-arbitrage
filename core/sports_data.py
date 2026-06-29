"""Sports consensus knowledge gate for the directional maker.

For a Kalshi championship-futures longshot (e.g. KXNBA-27-WAS = "Will Washington
win the title?"), keep the NO bet only when the de-vigged bookmaker CONSENSUS
(from The Odds API outrights) also says the team is a deep longshot. Mirrors the
weather/macro gate pattern; the "forecast" here is the consensus win probability.

Free-tier safe: The Odds API gives 500 credits/month (1 credit per league fetch).
This client caches per league and enforces a daily call cap, so a full month stays
well under 500.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ODDS_BASE = "https://api.the-odds-api.com/v4/sports"

# Kalshi championship-futures series prefix -> The Odds API outrights sport key.
KALSHI_SERIES_TO_ODDS: dict[str, str] = {
    "KXNBA": "basketball_nba_championship_winner",
    "KXMLB": "baseball_mlb_world_series_winner",
    "KXNHL": "icehockey_nhl_championship_winner",
    "KXNFL": "americanfootball_nfl_super_bowl_winner",
}


KALSHI_GAME_SERIES_TO_ODDS: dict[str, str] = {
    "KXMLBGAME": "baseball_mlb",
    "KXNBAGAME": "basketball_nba",
    "KXNHLGAME": "icehockey_nhl",
}


def kalshi_game_series_to_odds(ticker: str) -> Optional[str]:
    """Map a Kalshi per-game ticker to its Odds API h2h sport key (None if not a
    supported per-game market). Checks longest prefix first."""
    for series in sorted(KALSHI_GAME_SERIES_TO_ODDS, key=len, reverse=True):
        if ticker.startswith(series + "-"):
            return KALSHI_GAME_SERIES_TO_ODDS[series]
    return None


def kalshi_series_to_odds(ticker: str) -> Optional[str]:
    """Map a Kalshi futures ticker to its Odds API sport key (None if not a
    supported championship-futures market). Checks longest prefix first."""
    if not ticker:
        return None
    for series in sorted(KALSHI_SERIES_TO_ODDS, key=len, reverse=True):
        if ticker.startswith(series + "-"):
            return KALSHI_SERIES_TO_ODDS[series]
    return None


def consensus_probs(books: list) -> dict[str, float]:
    """De-vig each bookmaker's outright prices then average per team.

    ``books`` = list of per-bookmaker outcome lists ``[{"name", "price"}, ...]``
    (decimal odds). Returns ``{team: consensus_probability}`` in [0,1].
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for outcomes in books:
        raw = {}
        for o in outcomes:
            try:
                price = float(o["price"])
                if price > 0:
                    raw[o["name"]] = 1.0 / price
            except (KeyError, TypeError, ValueError):
                continue
        total = sum(raw.values())
        if total <= 0:
            continue
        for team, r in raw.items():
            sums[team] = sums.get(team, 0.0) + r / total  # de-vig
            counts[team] = counts.get(team, 0) + 1
    return {team: sums[team] / counts[team] for team in sums}


def match_team(kalshi_subtitle: str, probs: dict[str, float]) -> Optional[float]:
    """Return the consensus prob for the team the Kalshi sub-title refers to, but
    ONLY when exactly one Odds API team matches (unambiguous). Ambiguous (e.g.
    'New York' -> Knicks/Nets) or no-match -> None, so the candidate passes
    through ungated rather than being gated on a wrong team."""
    if not kalshi_subtitle:
        return None
    needle = kalshi_subtitle.strip().lower()
    hits = [v for team, v in probs.items() if needle in team.lower()]
    return hits[0] if len(hits) == 1 else None


def sports_gate_keep(consensus_prob: float, max_prob: float) -> bool:
    """KEEP the NO bet when the consensus win probability is at or below max_prob
    (the team is a consensus longshot)."""
    return consensus_prob <= max_prob


class SportsOddsClient:
    """The Odds API client: cached, daily-credit-capped outrights fetcher."""

    def __init__(self, http: Any, api_key: Optional[str], cache_ttl_s: int = 43200,
                 max_calls_per_day: int = 12) -> None:
        self._http = http
        self._key = api_key
        self._ttl = cache_ttl_s
        self._cache: dict[str, tuple[float, dict]] = {}
        self._max_calls = max_calls_per_day
        self._calls = 0
        self._call_date = ""

    async def championship_probs(self, ticker: str) -> dict[str, float]:
        """Consensus {team: prob} for the championship futures of ``ticker``'s
        league. {} if unsupported, no key, cache-miss-but-capped, or any error."""
        sport = kalshi_series_to_odds(ticker)
        if sport is None or not self._key:
            return {}
        now = time.monotonic()
        hit = self._cache.get(sport)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._call_date:
            self._call_date = today
            self._calls = 0
        if self._calls >= self._max_calls:
            logger.warning("[sports] daily Odds-API cap (%d) reached — skipping fetch", self._max_calls)
            return {}

        try:
            resp = await self._http.get(
                f"{_ODDS_BASE}/{sport}/odds/",
                params={"apiKey": self._key, "regions": "us",
                        "markets": "outrights", "oddsFormat": "decimal"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sports] Odds API %s error: %s", sport, exc)
            return {}
        finally:
            self._calls += 1

        books = []
        for ev in data or []:
            for bk in ev.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    if mkt.get("key") == "outrights":
                        books.append(mkt.get("outcomes", []))
        probs = consensus_probs(books)
        self._cache[sport] = (now, probs)
        return probs

    async def game_probs(self, ticker: str) -> dict[str, float]:
        """Consensus {team: prob} for per-game h2h markets of ``ticker``'s league.
        De-vigs each event independently and merges into a flat dict. {} if
        unsupported, no key, cache-miss-but-capped, or any error."""
        sport = kalshi_game_series_to_odds(ticker)
        if sport is None or not self._key:
            return {}
        now = time.monotonic()
        hit = self._cache.get(sport)
        if hit is not None and (now - hit[0]) < self._ttl:
            return hit[1]

        today = datetime.utcnow().strftime("%Y-%m-%d")
        if today != self._call_date:
            self._call_date = today
            self._calls = 0
        if self._calls >= self._max_calls:
            logger.warning("[sports] daily Odds-API cap (%d) reached — skipping fetch", self._max_calls)
            return {}

        try:
            resp = await self._http.get(
                f"{_ODDS_BASE}/{sport}/odds/",
                params={"apiKey": self._key, "regions": "us",
                        "markets": "h2h", "oddsFormat": "decimal"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sports] Odds API %s error: %s", sport, exc)
            return {}
        finally:
            self._calls += 1

        probs: dict[str, float] = {}
        for ev in data or []:
            event_books = []
            for bk in ev.get("bookmakers", []):
                for mkt in bk.get("markets", []):
                    if mkt.get("key") == "h2h":
                        event_books.append(mkt.get("outcomes", []))
            if event_books:
                probs.update(consensus_probs(event_books))
        self._cache[sport] = (now, probs)
        return probs
