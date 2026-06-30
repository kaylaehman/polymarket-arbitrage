"""
Unit tests for the v2 structured cross-platform market matcher.

Tests cover:
  - True match: same NFL game on both venues
  - Reject cross-sport: NFL vs MLB
  - Reject different game same sport: two different NFL games
  - Reject game-winner vs total: wrong market type
  - Reject KXMV parlay markets
  - Reject different season (same teams, different year)
  - Name-alias normalization (team abbreviations)
  - Outcome mapping: kalshi_yes_maps_to_poly_yes
  - World Cup tournament-winner matching
  - Empty result when only KXMV markets exist (current real-world state)
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
from unittest.mock import MagicMock

import pytest

from core.cross_platform_arb import (
    _MarketIdentity,
    MarketMatcher,
    MarketPair,
)


# ---------------------------------------------------------------------------
# Test helpers / fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakePolyMarket:
    """Minimal stand-in for polymarket_client.models.Market."""
    market_id: str
    question: str
    active: bool = True
    category: str = "sports"


@dataclass
class _FakeKalshiMarket:
    """Minimal stand-in for kalshi_client.models.KalshiMarket."""
    ticker: str
    title: str
    status: str = "open"

    @property
    def is_active(self) -> bool:
        return self.status in ("open", "active")


def run(coro):
    """Run a coroutine synchronously (pytest-asyncio alternative).

    Uses asyncio.run() so each call gets a fresh loop — hermetic under the full
    suite, where get_event_loop() raises once a prior async test has closed the
    thread's event loop (Python 3.12).
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# _MarketIdentity.from_kalshi tests
# ---------------------------------------------------------------------------

class TestMarketIdentityFromKalshi:
    def test_rejects_kxmv_parlay(self):
        """KXMV* multi-leg parlay markets must always be rejected."""
        for ticker in [
            "KXMVESPORTSMULTIGAMEEXTENDED-S2026123-ABC",
            "KXMVECROSSCATEGORY-S2026456-DEF",
            "KXMVESPORTSMULTIGAME-S2026789-GHI",
        ]:
            result = _MarketIdentity.from_kalshi(ticker, "yes Team A,yes Team B,yes Team C")
            assert result is None, f"Expected None for KXMV ticker {ticker!r}"

    def test_parses_nfl_game_ticker(self):
        """KXNFLGAME ticker should parse to sport=nfl, market_type=game_winner."""
        mid = _MarketIdentity.from_kalshi(
            "KXNFLGAME-26SEP13ATLPIT-ATL",
            "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?"
        )
        assert mid is not None
        assert mid.sport == "nfl"
        assert mid.market_type == "game_winner"
        assert mid.winner_team == "atlanta"
        assert mid.event_date == date(2026, 9, 13)

    def test_parses_nfl_game_ticker_pit_winner(self):
        """Same game, other team winning."""
        mid = _MarketIdentity.from_kalshi(
            "KXNFLGAME-26SEP13ATLPIT-PIT",
            "Will Pittsburgh win the Atlanta vs Pittsburgh Pro Football game?"
        )
        assert mid is not None
        assert mid.sport == "nfl"
        assert mid.winner_team == "pittsburgh"
        assert mid.event_date == date(2026, 9, 13)

    def test_parses_mlb_game_ticker(self):
        """KXMLBGAME ticker should parse to sport=mlb."""
        mid = _MarketIdentity.from_kalshi(
            "KXMLBGAME-26JUN202205LAAATH-LAA",
            "Los Angeles A vs A's Winner?"
        )
        assert mid is not None
        assert mid.sport == "mlb"
        assert mid.market_type == "game_winner"

    def test_rejects_crypto_price_market(self):
        """XRP price tickers should be rejected (not game markets)."""
        mid = _MarketIdentity.from_kalshi(
            "KXXRPD-26JUN1801-T1.8399",
            "Ripple price at Jun 18, 2026 at 1am EDT?"
        )
        assert mid is None

    def test_unknown_ticker_returns_none(self):
        """A ticker format we don't recognize returns None safely."""
        mid = _MarketIdentity.from_kalshi("RANDOMTICKER-XYZ", "Something unknown")
        assert mid is None


# ---------------------------------------------------------------------------
# _MarketIdentity.from_polymarket tests
# ---------------------------------------------------------------------------

class TestMarketIdentityFromPolymarket:
    def test_parses_nfl_slug(self):
        mid = _MarketIdentity.from_polymarket(
            "aec-nfl-atl-ne-2025-11-02",
            "Atlanta vs. New England",
            "sports",
        )
        assert mid is not None
        assert mid.sport == "nfl"
        assert mid.market_type == "game_winner"
        assert mid.event_date == date(2025, 11, 2)
        assert "atlanta" in mid.teams
        assert "new england" in mid.teams

    def test_parses_nfl_slug_kc_buf(self):
        mid = _MarketIdentity.from_polymarket(
            "aec-nfl-kc-buf-2025-11-02",
            "Kansas City vs. Buffalo",
            "sports",
        )
        assert mid is not None
        assert "kansas city" in mid.teams
        assert "buffalo" in mid.teams
        assert mid.event_date == date(2025, 11, 2)

    def test_parses_world_cup_question(self):
        mid = _MarketIdentity.from_polymarket(
            "will-spain-win-world-cup",
            "Will Spain win the 2026 FIFA World Cup?",
            "sports",
        )
        assert mid is not None
        assert mid.sport == "soccer"
        assert mid.market_type == "tournament_winner"
        assert "spain" in mid.teams

    def test_rejects_election_market(self):
        """Election markets don't parse to a known structured type."""
        mid = _MarketIdentity.from_polymarket(
            "will-trump-win-2028",
            "Will Trump win the 2028 US Presidential Election?",
            "politics",
        )
        assert mid is None  # no structured parser for elections yet

    def test_rejects_vague_crypto_market(self):
        mid = _MarketIdentity.from_polymarket(
            "will-bitcoin-hit-1m",
            "Will bitcoin hit $1m before GTA VI?",
            "crypto",
        )
        assert mid is None


# ---------------------------------------------------------------------------
# _MarketIdentity.matches tests
# ---------------------------------------------------------------------------

class TestMarketIdentityMatches:
    def _nfl_game(self, teams, evt_date, winner=None):
        return _MarketIdentity(
            sport="nfl",
            teams=frozenset(teams),
            event_date=evt_date,
            market_type="game_winner",
            winner_team=winner,
        )

    def test_same_game_matches(self):
        """Same NFL game on same date must match."""
        pm = self._nfl_game(["atlanta", "pittsburgh"], date(2026, 9, 13))
        k = self._nfl_game(["atlanta", "pittsburgh"], date(2026, 9, 13), winner="atlanta")
        is_match, conf, reason = pm.matches(k)
        assert is_match
        assert conf == 1.0

    def test_different_sport_rejected(self):
        """NFL vs MLB must be rejected even if team names overlap."""
        pm = _MarketIdentity(sport="nfl", teams=frozenset(["atlanta"]),
                             event_date=date(2026, 9, 13), market_type="game_winner")
        k = _MarketIdentity(sport="mlb", teams=frozenset(["atlanta"]),
                            event_date=date(2026, 9, 13), market_type="game_winner")
        is_match, _, reason = pm.matches(k)
        assert not is_match
        assert "sport mismatch" in reason

    def test_different_game_same_sport_rejected(self):
        """Atlanta vs Pittsburgh != Kansas City vs Buffalo."""
        pm = self._nfl_game(["atlanta", "pittsburgh"], date(2026, 9, 13))
        k = self._nfl_game(["kansas city", "buffalo"], date(2026, 9, 13), winner="kansas city")
        is_match, _, reason = pm.matches(k)
        assert not is_match
        assert "team mismatch" in reason

    def test_different_date_same_teams_rejected(self):
        """Same teams, different game date = different event."""
        pm = self._nfl_game(["atlanta", "pittsburgh"], date(2025, 11, 2))
        k = self._nfl_game(["atlanta", "pittsburgh"], date(2026, 9, 13), winner="atlanta")
        is_match, _, reason = pm.matches(k)
        assert not is_match
        assert "date mismatch" in reason

    def test_game_winner_vs_total_rejected(self):
        """game_winner vs total market type must be rejected."""
        pm = _MarketIdentity(sport="nfl", teams=frozenset(["atlanta", "pittsburgh"]),
                             event_date=date(2026, 9, 13), market_type="game_winner")
        k = _MarketIdentity(sport="nfl", teams=frozenset(["atlanta", "pittsburgh"]),
                            event_date=date(2026, 9, 13), market_type="total")
        is_match, _, reason = pm.matches(k)
        assert not is_match
        assert "market_type mismatch" in reason

    def test_no_date_on_one_side_doesnt_penalize(self):
        """If one market has no date extracted, date check passes."""
        pm = self._nfl_game(["atlanta", "pittsburgh"], None)
        k = self._nfl_game(["atlanta", "pittsburgh"], date(2026, 9, 13), winner="atlanta")
        is_match, conf, _ = pm.matches(k)
        assert is_match

    def test_world_cup_same_country_matches(self):
        """Same country tournament_winner must match."""
        pm = _MarketIdentity(sport="soccer", teams=frozenset(["spain"]),
                             event_date=None, market_type="tournament_winner")
        k = _MarketIdentity(sport="soccer", teams=frozenset(["spain"]),
                            event_date=None, market_type="tournament_winner")
        is_match, conf, _ = pm.matches(k)
        assert is_match

    def test_world_cup_different_country_rejected(self):
        """Brazil != Spain in tournament_winner."""
        pm = _MarketIdentity(sport="soccer", teams=frozenset(["brazil"]),
                             event_date=None, market_type="tournament_winner")
        k = _MarketIdentity(sport="soccer", teams=frozenset(["spain"]),
                            event_date=None, market_type="tournament_winner")
        is_match, _, reason = pm.matches(k)
        assert not is_match


# ---------------------------------------------------------------------------
# MarketMatcher.find_matches integration tests
# ---------------------------------------------------------------------------

class TestMarketMatcherFindMatches:
    """Integration tests for the full async find_matches pipeline."""

    def test_rejects_all_kxmv_returns_empty(self):
        """When all Kalshi markets are KXMV parlays, result is empty."""
        poly = [
            _FakePolyMarket("aec-nfl-atl-ne-2025-11-02", "Atlanta vs. New England"),
            _FakePolyMarket("aec-nfl-kc-buf-2025-11-02", "Kansas City vs. Buffalo"),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXMVESPORTSMULTIGAMEEXTENDED-S2026123-ABC",
                "yes Atlanta,yes New England,yes Kansas City,yes Buffalo",
            ),
            _FakeKalshiMarket(
                "KXMVECROSSCATEGORY-S2026456-DEF",
                "yes Kansas City,yes Buffalo,yes Over 2.5 goals scored",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert matches == [], (
            f"Expected 0 matches but got {len(matches)}: {[m.kalshi_ticker for m in matches]}"
        )

    def test_true_match_same_nfl_game(self):
        """Same NFL game on both venues (same date, same teams) must match."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-atl-pit-2026-09-13",
                "Atlanta vs. Pittsburgh",
            ),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert len(matches) == 1
        pair = matches[0]
        assert pair.polymarket_id == "aec-nfl-atl-pit-2026-09-13"
        assert pair.kalshi_ticker == "KXNFLGAME-26SEP13ATLPIT-ATL"
        assert pair.similarity_score == 1.0
        assert pair.category == "nfl"

    def test_outcome_mapping_first_team_wins(self):
        """Kalshi YES = ATL wins; PM.US YES = ATL wins (first in slug) → maps_to=True."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-atl-pit-2026-09-13",
                "Atlanta vs. Pittsburgh",
            ),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert len(matches) == 1
        # Atlanta is T1 in PM.US slug AND winner in Kalshi → same outcome
        assert matches[0].kalshi_yes_maps_to_poly_yes is True

    def test_outcome_mapping_second_team_wins(self):
        """Kalshi YES = PIT wins; PM.US YES = ATL wins (first in slug) → maps_to=False."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-atl-pit-2026-09-13",
                "Atlanta vs. Pittsburgh",
            ),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-PIT",
                "Will Pittsburgh win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert len(matches) == 1
        # Kalshi YES = PIT, PM.US YES = ATL → different outcomes
        assert matches[0].kalshi_yes_maps_to_poly_yes is False

    def test_cross_sport_rejected_nfl_vs_mlb(self):
        """NFL PM.US market must not match MLB Kalshi market even with shared city name."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-atl-pit-2026-09-13",
                "Atlanta vs. Pittsburgh",
            ),
        ]
        kalshi = [
            # MLB game: Atlanta Braves — different sport
            _FakeKalshiMarket(
                "KXMLBGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta Braves win the Atlanta vs Pittsburgh MLB game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert matches == [], "Cross-sport match should be rejected"

    def test_different_game_same_sport_rejected(self):
        """Kansas City vs Buffalo should NOT match Atlanta vs Pittsburgh."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-kc-buf-2025-11-02",
                "Kansas City vs. Buffalo",
            ),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert matches == [], "Different game must be rejected"

    def test_game_winner_vs_total_rejected(self):
        """Game winner PM.US market must not match a total (over/under) Kalshi market."""
        poly = [
            _FakePolyMarket(
                "aec-nfl-kc-buf-2025-11-02",
                "Kansas City vs. Buffalo",
            ),
        ]
        # A hypothetical Kalshi total market (if such existed in right format)
        # We test via identity parsing — 'total' type won't be generated by our parser
        # for KXNFLGAME tickers, so this test exercises the type gate path.
        poly_id = _MarketIdentity.from_polymarket(
            "aec-nfl-kc-buf-2025-11-02", "Kansas City vs. Buffalo", "sports"
        )
        kalshi_total_id = _MarketIdentity(
            sport="nfl",
            teams=frozenset(["kansas city", "buffalo"]),
            event_date=None,
            market_type="total",
        )
        is_match, _, reason = poly_id.matches(kalshi_total_id)
        assert not is_match
        assert "market_type mismatch" in reason

    def test_name_alias_normalization(self):
        """Abbreviation 'ne' → 'new england' must normalize correctly."""
        mid = _MarketIdentity.from_polymarket(
            "aec-nfl-atl-ne-2025-11-02",
            "Atlanta vs. New England",
            "sports",
        )
        assert mid is not None
        assert "new england" in mid.teams, f"Expected 'new england' in {mid.teams}"
        assert "atlanta" in mid.teams, f"Expected 'atlanta' in {mid.teams}"

    def test_inactive_kalshi_markets_excluded(self):
        """Closed/settled Kalshi markets must not be matched."""
        poly = [
            _FakePolyMarket("aec-nfl-atl-pit-2026-09-13", "Atlanta vs. Pittsburgh"),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
                status="settled",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert matches == [], "Settled Kalshi market should be excluded"

    def test_mixed_kxmv_and_individual_only_individual_matches(self):
        """With both KXMV and individual Kalshi markets, only the individual matches."""
        poly = [
            _FakePolyMarket("aec-nfl-atl-pit-2026-09-13", "Atlanta vs. Pittsburgh"),
        ]
        kalshi = [
            _FakeKalshiMarket(
                "KXMVESPORTSMULTIGAMEEXTENDED-S2026123-ABC",
                "yes Atlanta,yes Pittsburgh,yes Over 7.5 runs scored",
            ),
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert len(matches) == 1
        assert matches[0].kalshi_ticker == "KXNFLGAME-26SEP13ATLPIT-ATL"

    def test_different_season_rejected(self):
        """2025 NFL season PM.US market must not match 2026 season Kalshi market."""
        poly = [
            # 2025 season, Week 9
            _FakePolyMarket("aec-nfl-atl-ne-2025-11-02", "Atlanta vs. New England"),
        ]
        kalshi = [
            # 2026 season opener
            _FakeKalshiMarket(
                "KXNFLGAME-26SEP13ATLPIT-ATL",
                "Will Atlanta win the Atlanta vs Pittsburgh Pro Football game?",
            ),
        ]
        matcher = MarketMatcher()
        matches = run(matcher.find_matches(poly, kalshi))
        assert matches == [], (
            "Different NFL seasons (2025 vs 2026) should be rejected "
            "(different teams AND different dates)"
        )
