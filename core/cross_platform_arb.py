"""
Cross-Platform Arbitrage Engine
===============================

Detects arbitrage opportunities between Polymarket and Kalshi prediction markets.

When the same prediction is priced differently on both platforms, we can:
- Buy YES on cheaper platform, sell YES on expensive platform
- Or buy NO on cheaper platform, sell NO on expensive platform

MATCHER DESIGN (v2 — precision over recall)
--------------------------------------------
The v1 matcher used fuzzy string similarity and produced massive false positives
(e.g., "Atlanta vs. New England" NFL matched "Bryce Harper 2+ HR" MLB at 0.95).

v2 uses STRUCTURED IDENTITY matching:
  1. Reject Kalshi KXMV* parlay/multi-leg combo markets outright. These are
     structurally incompatible with individual binary outcome matching.
  2. For individual Kalshi markets (KXNFLGAME, KXMLBGAME, etc.), extract:
       - sport/league (NFL, NBA, MLB, ...)
       - participating teams (normalized + alias-expanded)
       - event date (from ticker encoding or title text)
       - market type (game-winner vs total vs player-prop vs tournament-winner)
  3. REQUIRE all four to agree before producing a match.
  4. Produce correct outcome mapping (which PM.US side == which Kalshi YES/NO).

Result: zero or very few matches when venues genuinely don't overlap, rather
than hundreds of spurious matches that would trigger unhedged two-leg orders.
"""

import asyncio
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Optional

from polymarket_client.models import Market, OrderBook, Opportunity, OpportunityType
from core.market_identity_macro import (
    MacroPoliticsIdentity,
    from_polymarket_macro,
    from_kalshi_macro,
    llm_equivalence_check,
)

if TYPE_CHECKING:
    # Type-only import; core never hard-imports the intelligence layer.
    from intelligence.signal import SignalSummary

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses (public API — unchanged from v1 so downstream consumers compile)
# ---------------------------------------------------------------------------

@dataclass
class _MarketProfile:
    """Precomputed matching features for a single market title (computed once)."""
    norm: str                       # normalized text (for fuzzy ratio)
    tokens: frozenset               # significant normalized tokens
    teams: list                     # canonical team names
    date: Optional[str]
    entities: frozenset             # key entities (names/numbers/terms)
    persons: frozenset              # politician/figure names mentioned
    actions: frozenset              # win/lose/poll/... action words
    sports_kw: frozenset            # nfl/nba/... keywords present
    crypto_kw: frozenset            # bitcoin/eth/... keywords present
    block_keys: frozenset           # tokens+teams used for candidate blocking


@dataclass
class MarketPair:
    """A matched pair of markets on Polymarket and Kalshi."""
    polymarket_id: str
    kalshi_ticker: str
    polymarket_question: str
    kalshi_title: str
    similarity_score: float
    category: str = ""

    # Timestamps
    matched_at: datetime = field(default_factory=datetime.utcnow)

    # v2 structured-match reason (human-readable, for validation)
    match_reason: str = ""
    # Which Kalshi side (YES/NO) corresponds to PM.US YES outcome
    kalshi_yes_maps_to_poly_yes: bool = True

    @property
    def pair_id(self) -> str:
        """Unique identifier for this pair."""
        return f"poly:{self.polymarket_id}|kalshi:{self.kalshi_ticker}"


@dataclass
class CrossPlatformOpportunity:
    """Arbitrage opportunity between Polymarket and Kalshi."""
    opportunity_id: str
    market_pair: MarketPair

    # Direction: which platform to buy/sell on
    buy_platform: str  # "polymarket" or "kalshi"
    sell_platform: str
    token: str  # "YES" or "NO"

    # Prices
    buy_price: float
    sell_price: float

    # Edge calculation
    gross_edge: float  # sell_price - buy_price
    net_edge: float    # After fees
    edge_pct: float    # As percentage

    # Sizing
    suggested_size: float = 0.0
    max_size: float = 0.0  # Limited by liquidity on both sides

    # Liquidity available
    buy_liquidity: float = 0.0
    sell_liquidity: float = 0.0

    # Metadata
    detected_at: datetime = field(default_factory=datetime.utcnow)

    # [Intelligence] Advisory AI signal — None if disabled/timed out. Annotate-only;
    # cross-platform arbs are flagged for human review, never auto-traded.
    signal: "Optional[SignalSummary]" = field(default=None, repr=False)

    def __str__(self) -> str:
        return (
            f"CrossPlatformArb: Buy {self.token} on {self.buy_platform} @ ${self.buy_price:.3f}, "
            f"Sell on {self.sell_platform} @ ${self.sell_price:.3f} | "
            f"Net Edge: {self.edge_pct:.2%}"
        )


# ---------------------------------------------------------------------------
# Structured identity helpers  (new in v2)
# ---------------------------------------------------------------------------

class _MarketIdentity:
    """
    Structured identity for one market: sport, teams, event_date, market_type.

    market_type values: "game_winner", "tournament_winner", "total", "spread",
                        "player_prop", "politics", "crypto", "other"
    """

    # Kalshi multi-leg parlay prefix — ALWAYS reject, never try to match
    _KXMV_PREFIX = re.compile(r'^KXMV', re.IGNORECASE)

    # Kalshi ticker patterns for structured extraction
    # NFL:  KXNFLGAME-26SEP13ATLPIT-ATL     → league=NFL, yr=26, month=SEP, day=13, winner=ATL
    # MLB:  KXMLBGAME-26JUN202205LAAATH-LAA → league=MLB, yr=26, month=JUN, day=20, time=2205 (optional)
    # The time component (4 digits) is optional and must be consumed but ignored.
    _KALSHI_GAME_TICKER = re.compile(
        r'^KX(?P<league>[A-Z]+)GAME-(?P<yr>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})'
        r'(?:\d{4})?'        # optional HHMM time suffix (MLB uses it, NFL doesn't)
        r'(?P<teams>[A-Z]+)-(?P<winner>[A-Z0-9]+)$',
        re.IGNORECASE,
    )

    # Month abbreviation → number
    _MONTHS = {
        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
        'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    }

    # PM.US market slug prefixes that encode sport+date
    # e.g. aec-nfl-atl-ne-2025-11-02  → NFL, ATL vs NE, 2025-11-02
    _POLY_NFL_SLUG = re.compile(
        r'^aec-nfl-(?P<t1>[a-z]+)-(?P<t2>[a-z]+)-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$'
    )
    _POLY_NBA_SLUG = re.compile(
        r'^aec-nba-(?P<t1>[a-z]+)-(?P<t2>[a-z]+)-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$'
    )
    _POLY_MLB_SLUG = re.compile(
        r'^aec-mlb-(?P<t1>[a-z]+)-(?P<t2>[a-z]+)-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})$'
    )

    # NFL team abbreviation → canonical + aliases
    _NFL_ABBREVS: dict[str, str] = {
        'atl': 'atlanta', 'ne': 'new england', 'no': 'new orleans',
        'lar': 'los angeles rams', 'lac': 'los angeles chargers',
        'lv': 'las vegas', 'nyg': 'new york giants', 'nyj': 'new york jets',
        'sf': 'san francisco', 'gb': 'green bay', 'kc': 'kansas city',
        'buf': 'buffalo', 'mia': 'miami', 'ind': 'indianapolis',
        'pit': 'pittsburgh', 'bal': 'baltimore', 'cle': 'cleveland',
        'cin': 'cincinnati', 'ten': 'tennessee', 'jax': 'jacksonville',
        'hou': 'houston', 'den': 'denver', 'oak': 'las vegas',
        'chi': 'chicago', 'det': 'detroit', 'min': 'minnesota',
        'sea': 'seattle', 'ari': 'arizona', 'dal': 'dallas',
        'phi': 'philadelphia', 'was': 'washington', 'car': 'carolina',
        'tb': 'tampa bay',
    }

    # Kalshi abbreviation used in game tickers → normalized
    _KALSHI_ABBREVS: dict[str, str] = {
        # NFL teams as used in KXNFLGAME tickers
        'ATL': 'atlanta', 'NE': 'new england', 'NO': 'new orleans',
        'LAR': 'los angeles rams', 'LAC': 'los angeles chargers',
        'LV': 'las vegas', 'NYG': 'new york giants', 'NYJ': 'new york jets',
        'SF': 'san francisco', 'GB': 'green bay', 'KC': 'kansas city',
        'BUF': 'buffalo', 'MIA': 'miami', 'IND': 'indianapolis',
        'PIT': 'pittsburgh', 'BAL': 'baltimore', 'CLE': 'cleveland',
        'CIN': 'cincinnati', 'TEN': 'tennessee', 'JAC': 'jacksonville',
        'HOU': 'houston', 'DEN': 'denver', 'CHI': 'chicago',
        'DET': 'detroit', 'MIN': 'minnesota', 'SEA': 'seattle',
        'ARI': 'arizona', 'DAL': 'dallas', 'PHI': 'philadelphia',
        'WAS': 'washington', 'CAR': 'carolina', 'TB': 'tampa bay',
        # NBA
        'BOS': 'boston', 'BKN': 'brooklyn', 'NYK': 'new york knicks',
        'MIL': 'milwaukee', 'MEM': 'memphis', 'SAS': 'san antonio',
        'OKC': 'oklahoma city', 'POR': 'portland', 'UTA': 'utah',
        'GSW': 'golden state', 'LAL': 'los angeles lakers',
        'LAC2': 'los angeles clippers', 'PHX': 'phoenix', 'SAC': 'sacramento',
        # MLB
        'LAA': 'los angeles angels', 'ATH': 'athletics',
        'NYY': 'new york yankees', 'NYM': 'new york mets',
        'BOS2': 'boston red sox',
    }

    def __init__(self, sport: str, teams: frozenset, event_date: Optional[date],
                 market_type: str, winner_team: Optional[str] = None,
                 macro_id: Optional["MacroPoliticsIdentity"] = None):
        self.sport = sport           # "nfl", "nba", "mlb", "soccer", "macro", "politics", "other"
        self.teams = teams           # frozenset of normalized team names
        self.event_date = event_date # date or None
        self.market_type = market_type
        self.winner_team = winner_team  # for game-winner markets: which team YES represents
        self.macro_id = macro_id     # set for macro/politics markets; None for sports

    @classmethod
    def from_kalshi(cls, ticker: str, title: str) -> Optional["_MarketIdentity"]:
        """
        Parse structured identity from a Kalshi market.
        Returns None if the market should be rejected (parlay, unparseable).
        """
        # Hard reject KXMV parlay/combo markets
        if cls._KXMV_PREFIX.match(ticker):
            return None

        # Try to parse KXNFLGAME / KXNBAGAME / KXMLBGAME style tickers
        m = cls._KALSHI_GAME_TICKER.match(ticker)
        if m:
            league = m.group('league').lower()  # "nfl", "nba", "mlb"
            month_str = m.group('month').upper()
            day = int(m.group('day'))
            month = cls._MONTHS.get(month_str, 0)
            if not month:
                return None

            # Year: tickers use 2-digit prefix captured as 'yr', e.g. "26" = 2026
            year = 2000 + int(m.group('yr'))

            try:
                evt_date = date(year, month, day)
            except ValueError:
                evt_date = None

            # Determine winner team from ticker suffix
            winner_abbrev = m.group('winner').upper()
            winner_norm = cls._KALSHI_ABBREVS.get(winner_abbrev, winner_abbrev.lower())

            # All teams in the game from the "teams" segment
            teams_segment = m.group('teams').upper()
            # The segment encodes team1+team2 concatenated, e.g. "ATLPIT" → ATL + PIT
            # We recover them from the title "X vs Y" pattern
            teams = cls._extract_teams_from_title(title, league)
            if not teams:
                # Fall back: winner + one unknown
                teams = frozenset([winner_norm])

            return cls(
                sport=league,
                teams=teams,
                event_date=evt_date,
                market_type="game_winner",
                winner_team=winner_norm,
            )

        # KXMLB series (season champion): "Will X win the 2026 Pro Baseball Championship?"
        if ticker.startswith('KXMLB-') and 'championship' in title.lower():
            teams = cls._extract_teams_from_title(title, 'mlb')
            return cls(sport='mlb', teams=teams, event_date=None,
                       market_type='tournament_winner', winner_team=None)

        # Macro / politics markets (KXFEDDECISION, KXSENATE, KXHOUSE, …)
        macro = from_kalshi_macro(ticker, title)
        if macro is not None:
            sport = 'politics' if macro.market_type == 'politics_control' else 'macro'
            return cls(
                sport=sport,
                teams=frozenset(),
                event_date=macro.event_date,
                market_type=macro.market_type,
                winner_team=None,
                macro_id=macro,
            )

        # Crypto price markets (KXXRPD etc): don't try to match with PM.US
        if re.match(r'^KX[A-Z]{2,5}D?-', ticker):
            return None

        return None

    @classmethod
    def from_polymarket(cls, market_id: str, question: str,
                        category: str) -> Optional["_MarketIdentity"]:
        """
        Parse structured identity from a PM.US market.
        Returns None if market type cannot be parsed precisely.
        """
        mid = market_id.lower()

        # aec-nfl-* game winner slugs
        for pattern, sport in [
            (cls._POLY_NFL_SLUG, 'nfl'),
            (cls._POLY_NBA_SLUG, 'nba'),
            (cls._POLY_MLB_SLUG, 'mlb'),
        ]:
            m = pattern.match(mid)
            if m:
                t1 = cls._NFL_ABBREVS.get(m.group('t1'), m.group('t1'))
                t2 = cls._NFL_ABBREVS.get(m.group('t2'), m.group('t2'))
                try:
                    evt_date = date(int(m.group('y')), int(m.group('m')), int(m.group('d')))
                except ValueError:
                    evt_date = None
                teams = frozenset([t1, t2])
                return cls(sport=sport, teams=teams, event_date=evt_date,
                           market_type='game_winner', winner_team=None)

        # World Cup / tournament winner: "Will X win the 2026 FIFA World Cup?"
        wc = re.match(
            r'will\s+(?P<country>.+?)\s+win\s+the\s+20\d\d\s+fifa\s+world\s+cup',
            question, re.IGNORECASE
        )
        if wc:
            country = wc.group('country').strip().lower()
            return cls(sport='soccer', teams=frozenset([country]), event_date=None,
                       market_type='tournament_winner', winner_team=None)

        # Macro / politics markets
        macro = from_polymarket_macro(market_id, question, category)
        if macro is not None:
            sport = 'politics' if macro.market_type == 'politics_control' else 'macro'
            return cls(
                sport=sport,
                teams=frozenset(),
                event_date=macro.event_date,
                market_type=macro.market_type,
                winner_team=None,
                macro_id=macro,
            )

        # For other market types we cannot yet extract a precise structured identity
        return None

    @classmethod
    def _extract_teams_from_title(cls, title: str, sport: str) -> frozenset:
        """
        Extract team names from a Kalshi game title.

        Handles two formats:
          "Will Kansas City win the Denver vs Kansas City Pro Football game?"
          "Denver vs Kansas City Winner?"
        """
        # Try "win the X vs Y" form first (avoids capturing "Will X win the")
        m = re.search(
            r'win\s+the\s+(?P<t1>[A-Za-z ]+?)\s+vs\s+(?P<t2>[A-Za-z ]+?)'
            r'(?:\s+Pro|\s+game|\s+MLB|\s+NBA|\?|$)',
            title, re.IGNORECASE
        )
        if not m:
            # Fallback: plain "X vs Y" without "win the" prefix
            m = re.search(
                r'^(?P<t1>[A-Za-z ]+?)\s+vs\s+(?P<t2>[A-Za-z ]+?)'
                r'(?:\s+Pro|\s+game|\s+MLB|\s+NBA|\s+Winner|\?|$)',
                title, re.IGNORECASE
            )
        if m:
            t1 = m.group('t1').strip().lower()
            t2 = m.group('t2').strip().lower()
            t1_norm = cls._normalize_team_name(t1, sport)
            t2_norm = cls._normalize_team_name(t2, sport)
            return frozenset([t1_norm, t2_norm])
        return frozenset()

    @classmethod
    def _normalize_team_name(cls, name: str, sport: str) -> str:
        """Normalize a team name to canonical form."""
        name = name.strip().lower()
        # Check PM.US abbreviation table first (covers most)
        if name in cls._NFL_ABBREVS:
            return cls._NFL_ABBREVS[name]
        # Common full-name cleanup
        replacements = {
            'new york g': 'new york giants',
            'new york j': 'new york jets',
            'los angeles r': 'los angeles rams',
            'los angeles c': 'los angeles chargers',
            'los angeles a': 'los angeles angels',
        }
        return replacements.get(name, name)

    def matches(self, other: "_MarketIdentity") -> tuple[bool, float, str]:
        """
        Compare two market identities.

        Returns (is_match, confidence, reason_string).
        All criteria must agree; any mismatch returns False immediately.

        For macro/politics markets (macro_id set on both), delegates to
        MacroPoliticsIdentity.matches() which handles the schema-specific logic.
        """
        # 1. Sport must match exactly
        if self.sport != other.sport:
            return False, 0.0, f"sport mismatch: {self.sport!r} vs {other.sport!r}"

        # 2. Market type must match exactly
        if self.market_type != other.market_type:
            return False, 0.0, (
                f"market_type mismatch: {self.market_type!r} vs {other.market_type!r}"
            )

        # 3a. Macro/politics: delegate to MacroPoliticsIdentity.matches()
        if self.macro_id is not None and other.macro_id is not None:
            return self.macro_id.matches(other.macro_id)

        # 3b. Sports: Teams must agree (full intersection for game_winner)
        if self.market_type == 'game_winner':
            if not self.teams or not other.teams:
                return False, 0.0, "missing teams"
            if self.teams != other.teams:
                return False, 0.0, f"team mismatch: {self.teams} vs {other.teams}"

        elif self.market_type == 'tournament_winner':
            # Each market specifies one team; they must be the same team
            if not self.teams or not other.teams:
                return False, 0.0, "missing tournament team"
            if not (self.teams & other.teams):
                return False, 0.0, f"tournament team mismatch: {self.teams} vs {other.teams}"

        # 4. Event date: if both have dates, they must match exactly (game-specific)
        if self.event_date and other.event_date:
            if self.event_date != other.event_date:
                return False, 0.0, (
                    f"date mismatch: {self.event_date} vs {other.event_date}"
                )

        # All criteria agree
        confidence = 1.0
        reason = (
            f"sport={self.sport} market_type={self.market_type} "
            f"teams={self.teams} date={self.event_date or other.event_date}"
        )
        return True, confidence, reason


# ---------------------------------------------------------------------------
# Main matcher class
# ---------------------------------------------------------------------------

class MarketMatcher:
    """
    Matches markets between Polymarket.US and Kalshi.

    v2: Structured identity matching only. No fuzzy string similarity.
    Precision over recall: returns 0 correct pairs rather than N wrong ones.

    Public interface (class name, method names, return types) is UNCHANGED
    from v1 so run_with_dashboard.py, cross_platform_monitor.py, and the
    executor all continue to work without modification.
    """

    # Keep v1 class-level team tables so other code that imported them still works
    NOISE_WORDS = {
        "will", "the", "a", "an", "be", "to", "in", "on", "by", "at",
        "what", "who", "which", "when", "is", "are", "was", "were",
        "market", "prediction", "bet", "odds", "win", "winner"
    }

    NFL_TEAMS = {
        "arizona cardinals": ["cardinals", "arizona", "ari"],
        "atlanta falcons": ["falcons", "atlanta", "atl"],
        "baltimore ravens": ["ravens", "baltimore", "bal"],
        "buffalo bills": ["bills", "buffalo", "buf"],
        "carolina panthers": ["panthers", "carolina", "car"],
        "chicago bears": ["bears", "chicago", "chi"],
        "cincinnati bengals": ["bengals", "cincinnati", "cin"],
        "cleveland browns": ["browns", "cleveland", "cle"],
        "dallas cowboys": ["cowboys", "dallas", "dal"],
        "denver broncos": ["broncos", "denver", "den"],
        "detroit lions": ["lions", "detroit", "det"],
        "green bay packers": ["packers", "green bay", "gb"],
        "houston texans": ["texans", "houston", "hou"],
        "indianapolis colts": ["colts", "indianapolis", "ind"],
        "jacksonville jaguars": ["jaguars", "jacksonville", "jax"],
        "kansas city chiefs": ["chiefs", "kansas city", "kc"],
        "las vegas raiders": ["raiders", "las vegas", "lv"],
        "los angeles chargers": ["chargers", "la chargers", "lac"],
        "los angeles rams": ["rams", "la rams", "lar"],
        "miami dolphins": ["dolphins", "miami", "mia"],
        "minnesota vikings": ["vikings", "minnesota", "min"],
        "new england patriots": ["patriots", "new england", "ne"],
        "new orleans saints": ["saints", "new orleans", "no"],
        "new york giants": ["giants", "ny giants", "nyg"],
        "new york jets": ["jets", "ny jets", "nyj"],
        "philadelphia eagles": ["eagles", "philadelphia", "phi"],
        "pittsburgh steelers": ["steelers", "pittsburgh", "pit"],
        "san francisco 49ers": ["49ers", "san francisco", "sf"],
        "seattle seahawks": ["seahawks", "seattle", "sea"],
        "tampa bay buccaneers": ["buccaneers", "tampa bay", "tb"],
        "tennessee titans": ["titans", "tennessee", "ten"],
        "washington commanders": ["commanders", "washington", "was"],
    }

    NBA_TEAMS = {
        "boston celtics": ["celtics", "boston"],
        "brooklyn nets": ["nets", "brooklyn"],
        "new york knicks": ["knicks", "new york"],
        "philadelphia 76ers": ["76ers", "sixers", "philadelphia"],
        "toronto raptors": ["raptors", "toronto"],
        "chicago bulls": ["bulls", "chicago"],
        "cleveland cavaliers": ["cavaliers", "cavs", "cleveland"],
        "detroit pistons": ["pistons", "detroit"],
        "indiana pacers": ["pacers", "indiana"],
        "milwaukee bucks": ["bucks", "milwaukee"],
        "atlanta hawks": ["hawks", "atlanta"],
        "charlotte hornets": ["hornets", "charlotte"],
        "miami heat": ["heat", "miami"],
        "orlando magic": ["magic", "orlando"],
        "washington wizards": ["wizards", "washington"],
        "denver nuggets": ["nuggets", "denver"],
        "minnesota timberwolves": ["timberwolves", "wolves", "minnesota"],
        "oklahoma city thunder": ["thunder", "okc"],
        "portland trail blazers": ["blazers", "portland"],
        "utah jazz": ["jazz", "utah"],
        "golden state warriors": ["warriors", "golden state"],
        "los angeles clippers": ["clippers", "la clippers"],
        "los angeles lakers": ["lakers", "la lakers"],
        "phoenix suns": ["suns", "phoenix"],
        "sacramento kings": ["kings", "sacramento"],
        "dallas mavericks": ["mavericks", "mavs", "dallas"],
        "houston rockets": ["rockets", "houston"],
        "memphis grizzlies": ["grizzlies", "memphis"],
        "new orleans pelicans": ["pelicans", "new orleans"],
        "san antonio spurs": ["spurs", "san antonio"],
    }

    def __init__(self, min_similarity: float = 0.5):
        """
        Initialize matcher.

        Args:
            min_similarity: kept for interface compatibility; v2 uses structured
                            matching, this threshold is not applied to fuzzy scores.
        """
        self.min_similarity = min_similarity
        self._matched_pairs: dict[str, MarketPair] = {}

        # Build reverse lookup for team names (v1 compat, used by _categorize_market)
        self._team_lookup: dict[str, str] = {}
        for full_name, variants in {**self.NFL_TEAMS, **self.NBA_TEAMS}.items():
            self._team_lookup[full_name] = full_name
            for variant in variants:
                self._team_lookup[variant.lower()] = full_name

    # ---- v1 compat helpers (kept; downstream code may call them) -------------

    def normalize_text(self, text: str) -> str:
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)
        words = text.split()
        words = [w for w in words if w not in self.NOISE_WORDS]
        return ' '.join(words)

    def extract_teams(self, text: str) -> list[str]:
        text_lower = text.lower()
        found_teams: list[str] = []
        for team_key in sorted(self._team_lookup.keys(), key=len, reverse=True):
            if team_key in text_lower:
                canonical = self._team_lookup[team_key]
                if canonical not in found_teams:
                    found_teams.append(canonical)
                    text_lower = text_lower.replace(team_key, "")
        return found_teams

    def extract_key_entities(self, text: str) -> set[str]:
        entities: set[str] = set()
        entities.update(re.findall(r'\d+(?:\.\d+)?%?', text))
        entities.update(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text))
        for term in ["trump", "biden", "republican", "democrat", "harris",
                     "bitcoin", "btc", "ethereum", "eth", "solana"]:
            if term in text.lower():
                entities.add(term)
        return entities

    def extract_date(self, text: str) -> Optional[str]:
        months = {
            'jan': '01', 'february': '02', 'feb': '02', 'march': '03', 'mar': '03',
            'april': '04', 'apr': '04', 'may': '05', 'june': '06', 'jun': '06',
            'july': '07', 'jul': '07', 'august': '08', 'aug': '08',
            'september': '09', 'sep': '09', 'october': '10', 'oct': '10',
            'november': '11', 'nov': '11', 'december': '12', 'dec': '12',
            'january': '01',
        }
        text_lower = text.lower()
        for month_name, month_num in months.items():
            pattern = rf'{month_name}\.?\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?'
            match = re.search(pattern, text_lower)
            if match:
                day = match.group(1).zfill(2)
                year = match.group(2) or '2024'
                return f"{year}-{month_num}-{day}"
        match = re.search(r'(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})', text)
        if match:
            month = match.group(1).zfill(2)
            day = match.group(2).zfill(2)
            year = match.group(3)
            if len(year) == 2:
                year = '20' + year
            return f"{year}-{month}-{day}"
        return None

    def dates_match(self, date1: Optional[str], date2: Optional[str]) -> bool:
        if not date1 or not date2:
            return True
        return date1 == date2

    def _categorize_market(self, text: str) -> str:
        text_lower = text.lower()
        if any(x in text_lower for x in ['trump', 'biden', 'harris', 'president',
                'election', 'democrat', 'republican', 'congress', 'senate',
                'governor', 'mayor', 'vote', 'nominee', 'primary', 'presidential',
                'prime minister', 'parliament']):
            return 'politics'
        if any(x in text_lower for x in ['bitcoin', 'btc', 'ethereum', 'eth',
                'crypto', 'token', 'solana', 'sol', 'blockchain', 'defi', 'nft']):
            return 'crypto'
        if any(x in text_lower for x in ['fed', 'interest rate', 'inflation', 'gdp',
                'recession', 'stock', 'nasdaq', 'dow', 's&p', 'treasury', 'tariff',
                'federal reserve']):
            return 'finance'
        sports_keywords = ['nfl', 'nba', 'mlb', 'nhl', 'premier league',
            'champions league', 'super bowl', 'playoff', 'la liga', 'soccer',
            ' fc', 'basketball team', 'football team', 'hockey', 'world cup',
            'stanley cup']
        if any(x in text_lower for x in sports_keywords):
            return 'sports'
        if any(x in text_lower for x in self._team_lookup.keys()):
            return 'sports'
        if any(x in text_lower for x in ['oscar', 'grammy', 'emmy', 'movie', 'film',
                'album', 'artist', 'actor', 'actress', 'netflix', 'spotify']):
            return 'entertainment'
        if any(x in text_lower for x in ['ai ', 'openai', 'gpt', 'google', 'apple',
                'microsoft', 'tesla', 'spacex', 'nvidia']):
            return 'tech'
        return 'other'

    # ---- v2 core: structured matching ----------------------------------------

    _PERSON_PATTERNS = [
        r'\b(trump|biden|harris|desantis|obama|pence)\b',
        r'\b(musk|zuckerberg|bezos|gates)\b',
        r'\b(powell|yellen)\b',
    ]
    _ACTION_RE = re.compile(
        r'\b(win|lose|approve|poll|elect|resign|indicted?|convicted?)\w*\b'
    )
    _SPORT_KEYWORDS = ("nfl", "nba", "mlb", "nhl", "football", "basketball",
                       "baseball", "hockey")
    _CRYPTO_KEYWORDS = ("bitcoin", "btc", "ethereum", "eth", "solana", "sol")

    def build_profile(self, text: str) -> _MarketProfile:
        """Compute all matching features (kept for interface compat; used in category-blocking)."""
        text = text or ""
        lower = text.lower()
        norm = self.normalize_text(text)
        tokens = frozenset(w for w in norm.split() if len(w) >= 3)
        teams = self.extract_teams(text)
        evt_date = self.extract_date(text)
        entities = frozenset(self.extract_key_entities(text))
        persons: set[str] = set()
        for pat in self._PERSON_PATTERNS:
            persons.update(re.findall(pat, lower))
        actions = frozenset(self._ACTION_RE.findall(lower))
        sports_kw = frozenset(s for s in self._SPORT_KEYWORDS if s in lower)
        crypto_kw = frozenset(c for c in self._CRYPTO_KEYWORDS if c in lower)
        block_keys = frozenset(tokens | set(teams))
        return _MarketProfile(
            norm=norm, tokens=tokens, teams=teams, date=evt_date,
            entities=entities, persons=frozenset(persons), actions=actions,
            sports_kw=sports_kw, crypto_kw=crypto_kw, block_keys=block_keys,
        )

    def _try_structured_match(
        self,
        poly_market: Market,
        kalshi_market,       # KalshiMarket
    ) -> Optional[MarketPair]:
        """
        Attempt a STRUCTURED IDENTITY match between one PM.US and one Kalshi market.

        Returns a MarketPair if and only if the markets represent the same
        underlying binary event, with all criteria in agreement.  Returns None
        otherwise.

        For macro/politics markets the outcome mapping is always YES==YES because:
          - Fed Decision: PM.US "maintains" slug vs Kalshi H0 both mean "Fed holds";
            YES on each side resolves identically.
          - Politics: PM.US "-rep" slug vs Kalshi "-R" market; YES on each means
            "Republicans win the chamber".
        """
        # Parse Kalshi identity — reject KXMV parlays immediately
        kalshi_id = _MarketIdentity.from_kalshi(
            kalshi_market.ticker, kalshi_market.title
        )
        if kalshi_id is None:
            return None

        # Parse PM.US identity
        poly_id = _MarketIdentity.from_polymarket(
            poly_market.market_id, poly_market.question, poly_market.category
        )
        if poly_id is None:
            return None

        is_match, confidence, reason = poly_id.matches(kalshi_id)
        if not is_match:
            return None

        # Determine outcome mapping for sports game-winners:
        # PM.US aec-nfl-T1-T2-date: YES = T1 wins
        # Kalshi KXNFLGAME-...-WINNER: YES = WINNER wins
        # If kalshi_id.winner_team == PM.US T1, Kalshi YES == PM.US YES.
        kalshi_yes_maps_to_poly_yes = True
        if (kalshi_id.market_type == 'game_winner' and kalshi_id.winner_team
                and kalshi_id.sport in ('nfl', 'nba', 'mlb')):
            mid = poly_market.market_id.lower()
            for pattern in [
                _MarketIdentity._POLY_NFL_SLUG,
                _MarketIdentity._POLY_NBA_SLUG,
                _MarketIdentity._POLY_MLB_SLUG,
            ]:
                pm = pattern.match(mid)
                if pm:
                    t1_abbrev = pm.group('t1')
                    t1_norm = _MarketIdentity._NFL_ABBREVS.get(t1_abbrev, t1_abbrev)
                    kalshi_yes_maps_to_poly_yes = (kalshi_id.winner_team == t1_norm)
                    break
        # For macro/politics markets the structural match guarantees same-action,
        # so YES always maps to YES (both sides independently encode the same outcome
        # token — "maintains", "cut25bps", "republican", etc.).

        pair = MarketPair(
            polymarket_id=poly_market.market_id,
            kalshi_ticker=kalshi_market.ticker,
            polymarket_question=poly_market.question,
            kalshi_title=kalshi_market.title,
            similarity_score=confidence,
            category=poly_id.sport,
            match_reason=reason,
            kalshi_yes_maps_to_poly_yes=kalshi_yes_maps_to_poly_yes,
        )
        return pair

    async def find_matches(
        self,
        polymarket_markets: list[Market],
        kalshi_markets: list,   # list[KalshiMarket]
        on_progress: Optional[callable] = None,
        llm_gate=None,          # Optional AIAnalyzer for equivalence confirmation
    ) -> list[MarketPair]:
        """
        Find matching markets between platforms using STRUCTURED IDENTITY only.

        v2 algorithm:
          1. Pre-filter: discard all KXMV* Kalshi markets (parlays, never matchable).
          2. For remaining Kalshi individual markets, parse structured identity once.
          3. For each PM.US market, parse structured identity and probe only the
             Kalshi markets of the same sport/type (fast blocking).
          4. Require all four criteria (sport, market_type, teams, date) to agree.
          5. Return only confirmed pairs.

        Performance: the pre-filter typically eliminates >99% of Kalshi open
        markets (right now 100% are KXMV).  The remainder is a small set of
        individual game/tournament markets that can be compared in O(N*M/C) where
        C is the number of sport categories.

        Args:
            polymarket_markets: List of Polymarket markets
            kalshi_markets: List of Kalshi markets
            on_progress: Optional callback(checked, total, matches_found)

        Returns:
            List of verified matched market pairs
        """
        matches: list[MarketPair] = []

        active_poly = [m for m in polymarket_markets if m.active]
        active_kalshi = [m for m in kalshi_markets if m.is_active]

        logger.info(
            f"v2 structured matcher: {len(active_poly)} PM.US + "
            f"{len(active_kalshi)} Kalshi active markets"
        )

        # Step 1: Pre-filter Kalshi — reject all KXMV parlay markets immediately
        individual_kalshi = [
            m for m in active_kalshi
            if not m.ticker.upper().startswith('KXMV')
        ]
        rejected_kxmv = len(active_kalshi) - len(individual_kalshi)
        logger.info(
            f"Kalshi pre-filter: rejected {rejected_kxmv} KXMV parlay/combo markets, "
            f"{len(individual_kalshi)} individual markets remain"
        )

        if not individual_kalshi:
            logger.info(
                "No individual Kalshi markets found (only KXMV parlays are open). "
                "Result: 0 matched pairs. This is the correct honest outcome — "
                "Kalshi's current open market universe does not overlap with PM.US."
            )
            return []

        # Step 2: Parse structured identities for individual Kalshi markets once
        kalshi_parsed: list[tuple] = []  # (KalshiMarket, _MarketIdentity)
        for km in individual_kalshi:
            kid = _MarketIdentity.from_kalshi(km.ticker, km.title)
            if kid is not None:
                kalshi_parsed.append((km, kid))

        logger.info(
            f"Kalshi structured-parse: {len(kalshi_parsed)}/{len(individual_kalshi)} "
            f"yielded valid identities"
        )

        if not kalshi_parsed:
            logger.info("0 Kalshi markets produced valid structured identities. Result: 0 pairs.")
            return []

        # Step 3: Build blocking index by (sport, market_type) for Kalshi
        kalshi_index: dict[tuple[str, str], list] = defaultdict(list)
        for km, kid in kalshi_parsed:
            kalshi_index[(kid.sport, kid.market_type)].append((km, kid))

        # Step 4: For each PM.US market, probe matching Kalshi bucket
        total_pm = len(active_poly)
        checked = 0
        for poly_market in active_poly:
            pid = _MarketIdentity.from_polymarket(
                poly_market.market_id, poly_market.question, poly_market.category
            )
            if pid is None:
                checked += 1
                continue

            bucket = kalshi_index.get((pid.sport, pid.market_type), [])
            for km, kid in bucket:
                is_match, confidence, reason = pid.matches(kid)
                if is_match:
                    pair = self._try_structured_match(poly_market, km)
                    if pair:
                        # Optional LLM equivalence gate for macro/politics pairs.
                        # If the gate is configured and returns equivalent=False,
                        # we skip the pair (false hedge prevention).
                        # Gate errors always degrade to accepting the structural match.
                        if llm_gate is not None and pid.macro_id is not None:
                            llm_ok, llm_yes_map, llm_reason = await llm_equivalence_check(
                                poly_market.question, km.title, llm_gate
                            )
                            if not llm_ok:
                                logger.info(
                                    f"LLM gate rejected structural match: "
                                    f"'{poly_market.question[:50]}' <-> '{km.title[:50]}' | "
                                    f"{llm_reason}"
                                )
                                continue
                            # LLM may refine the YES mapping
                            pair.kalshi_yes_maps_to_poly_yes = llm_yes_map
                            pair.match_reason = f"{reason} | LLM: {llm_reason}"

                        matches.append(pair)
                        self._matched_pairs[pair.pair_id] = pair
                        logger.info(
                            f"STRUCTURED MATCH: '{poly_market.question[:50]}' <-> "
                            f"'{km.title[:50]}' | {reason}"
                        )
                        # Each PM.US market matches at most one Kalshi market
                        break

            checked += 1
            if checked % 500 == 0:
                await asyncio.sleep(0)
                if on_progress:
                    try:
                        on_progress(checked, total_pm, len(matches))
                    except Exception:
                        pass

        logger.info(
            f"=== STRUCTURED MATCHING COMPLETE: {len(matches)} verified pairs "
            f"(checked {checked} PM.US markets against {len(kalshi_parsed)} Kalshi) ==="
        )
        return matches

    def get_cached_pairs(self) -> list[MarketPair]:
        """Get all cached market pairs."""
        return list(self._matched_pairs.values())


# ---------------------------------------------------------------------------
# CrossPlatformArbEngine (UNCHANGED from v1 — only the MarketMatcher changed)
# ---------------------------------------------------------------------------

class CrossPlatformArbEngine:
    """
    Detects arbitrage opportunities between Polymarket and Kalshi.

    Monitors matched market pairs and alerts when prices diverge enough
    to create profitable cross-platform arbitrage.
    """

    def __init__(
        self,
        min_edge: float = 0.02,
        polymarket_taker_fee: float = 0.015,
        kalshi_taker_fee: float = 0.01,
        gas_cost: float = 0.02,
    ):
        self.min_edge = min_edge
        self.polymarket_taker_fee = polymarket_taker_fee
        self.kalshi_taker_fee = kalshi_taker_fee
        self.gas_cost = gas_cost

        self.matcher = MarketMatcher()
        self._opportunities: list[CrossPlatformOpportunity] = []
        self._opportunity_count = 0

    def check_arbitrage(
        self,
        market_pair: MarketPair,
        polymarket_ob: OrderBook,
        kalshi_ob: OrderBook,
    ) -> Optional[CrossPlatformOpportunity]:
        """
        Check for arbitrage opportunity between a matched market pair.

        Uses the outcome mapping in market_pair.kalshi_yes_maps_to_poly_yes to
        ensure YES on one platform is compared to the corresponding YES on the
        other (not the inverse outcome).
        """
        poly_yes_ask = polymarket_ob.best_ask_yes
        poly_yes_bid = polymarket_ob.best_bid_yes
        poly_no_ask = polymarket_ob.best_ask_no
        poly_no_bid = polymarket_ob.best_bid_no

        # Apply outcome mapping: if Kalshi YES != PM.US YES, swap Kalshi sides
        if market_pair.kalshi_yes_maps_to_poly_yes:
            kalshi_yes_ask = kalshi_ob.best_ask_yes
            kalshi_yes_bid = kalshi_ob.best_bid_yes
            kalshi_no_ask = kalshi_ob.best_ask_no
            kalshi_no_bid = kalshi_ob.best_bid_no
        else:
            # Kalshi YES = PM.US NO; flip for comparison
            kalshi_yes_ask = kalshi_ob.best_ask_no
            kalshi_yes_bid = kalshi_ob.best_bid_no
            kalshi_no_ask = kalshi_ob.best_ask_yes
            kalshi_no_bid = kalshi_ob.best_bid_yes

        if not all([poly_yes_ask, poly_yes_bid, kalshi_yes_ask, kalshi_yes_bid]):
            return None

        best_opp = None
        best_net_edge = 0.0

        # 1. Buy YES on Polymarket, sell YES on Kalshi
        if poly_yes_ask and kalshi_yes_bid:
            gross = kalshi_yes_bid - poly_yes_ask
            fees = (poly_yes_ask * self.polymarket_taker_fee +
                    kalshi_yes_bid * self.kalshi_taker_fee +
                    self.gas_cost * 2)
            net = gross - fees
            if net > best_net_edge and net >= self.min_edge:
                best_net_edge = net
                best_opp = self._create_opportunity(
                    market_pair=market_pair,
                    buy_platform="polymarket", sell_platform="kalshi",
                    token="YES", buy_price=poly_yes_ask, sell_price=kalshi_yes_bid,
                    gross_edge=gross, net_edge=net,
                    buy_liquidity=polymarket_ob.yes.asks.best_size or 0,
                    sell_liquidity=kalshi_ob.yes.bids.best_size or 0,
                )

        # 2. Buy YES on Kalshi, sell YES on Polymarket
        if kalshi_yes_ask and poly_yes_bid:
            gross = poly_yes_bid - kalshi_yes_ask
            fees = (kalshi_yes_ask * self.kalshi_taker_fee +
                    poly_yes_bid * self.polymarket_taker_fee +
                    self.gas_cost * 2)
            net = gross - fees
            if net > best_net_edge and net >= self.min_edge:
                best_net_edge = net
                best_opp = self._create_opportunity(
                    market_pair=market_pair,
                    buy_platform="kalshi", sell_platform="polymarket",
                    token="YES", buy_price=kalshi_yes_ask, sell_price=poly_yes_bid,
                    gross_edge=gross, net_edge=net,
                    buy_liquidity=kalshi_ob.yes.asks.best_size or 0,
                    sell_liquidity=polymarket_ob.yes.bids.best_size or 0,
                )

        # 3. Buy NO on Polymarket, sell NO on Kalshi
        if poly_no_ask and kalshi_no_bid:
            gross = kalshi_no_bid - poly_no_ask
            fees = (poly_no_ask * self.polymarket_taker_fee +
                    kalshi_no_bid * self.kalshi_taker_fee +
                    self.gas_cost * 2)
            net = gross - fees
            if net > best_net_edge and net >= self.min_edge:
                best_net_edge = net
                best_opp = self._create_opportunity(
                    market_pair=market_pair,
                    buy_platform="polymarket", sell_platform="kalshi",
                    token="NO", buy_price=poly_no_ask, sell_price=kalshi_no_bid,
                    gross_edge=gross, net_edge=net,
                    buy_liquidity=polymarket_ob.no.asks.best_size or 0,
                    sell_liquidity=kalshi_ob.no.bids.best_size or 0,
                )

        # 4. Buy NO on Kalshi, sell NO on Polymarket
        if kalshi_no_ask and poly_no_bid:
            gross = poly_no_bid - kalshi_no_ask
            fees = (kalshi_no_ask * self.kalshi_taker_fee +
                    poly_no_bid * self.polymarket_taker_fee +
                    self.gas_cost * 2)
            net = gross - fees
            if net > best_net_edge and net >= self.min_edge:
                best_net_edge = net
                best_opp = self._create_opportunity(
                    market_pair=market_pair,
                    buy_platform="kalshi", sell_platform="polymarket",
                    token="NO", buy_price=kalshi_no_ask, sell_price=poly_no_bid,
                    gross_edge=gross, net_edge=net,
                    buy_liquidity=kalshi_ob.no.asks.best_size or 0,
                    sell_liquidity=polymarket_ob.no.bids.best_size or 0,
                )

        if best_opp:
            self._opportunities.append(best_opp)
            logger.info(f"CROSS-PLATFORM ARB: {best_opp}")

        return best_opp

    def _create_opportunity(
        self,
        market_pair: MarketPair,
        buy_platform: str,
        sell_platform: str,
        token: str,
        buy_price: float,
        sell_price: float,
        gross_edge: float,
        net_edge: float,
        buy_liquidity: float,
        sell_liquidity: float,
    ) -> CrossPlatformOpportunity:
        self._opportunity_count += 1
        max_size = min(buy_liquidity, sell_liquidity)
        suggested_size = min(max_size, 100.0)
        return CrossPlatformOpportunity(
            opportunity_id=f"xplat_{self._opportunity_count}",
            market_pair=market_pair,
            buy_platform=buy_platform,
            sell_platform=sell_platform,
            token=token,
            buy_price=buy_price,
            sell_price=sell_price,
            gross_edge=gross_edge,
            net_edge=net_edge,
            edge_pct=net_edge / buy_price if buy_price > 0 else 0,
            suggested_size=suggested_size,
            max_size=max_size,
            buy_liquidity=buy_liquidity,
            sell_liquidity=sell_liquidity,
        )

    def get_recent_opportunities(self, limit: int = 50) -> list[CrossPlatformOpportunity]:
        return self._opportunities[-limit:]

    def get_stats(self) -> dict:
        return {
            "total_opportunities": len(self._opportunities),
            "matched_pairs": len(self.matcher.get_cached_pairs()),
            "avg_edge": (
                sum(o.net_edge for o in self._opportunities) / len(self._opportunities)
                if self._opportunities else 0
            ),
        }
