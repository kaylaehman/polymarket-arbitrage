"""Sports consensus gate: keep a NO longshot only when the bookmaker consensus
also says the team is a longshot. Mirrors the weather/macro gate pattern but the
"forecast" is the de-vigged consensus championship probability from The Odds API.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.sports_data import (
    consensus_probs, match_team, sports_gate_keep,
    kalshi_series_to_odds, SportsOddsClient,
)


# ── de-vig consensus ────────────────────────────────────────────────────────

def test_consensus_devig_single_book():
    # Two outcomes priced 2.0 and 2.0 -> raw 0.5+0.5=1.0 (no vig) -> 0.5/0.5
    books = [[{"name": "A", "price": 2.0}, {"name": "B", "price": 2.0}]]
    p = consensus_probs(books)
    assert p["A"] == pytest.approx(0.5)
    assert p["B"] == pytest.approx(0.5)


def test_consensus_devig_removes_vig():
    # 1.5 & 2.5 -> raw 0.6667 + 0.4 = 1.0667 (vig) -> normalized
    books = [[{"name": "Fav", "price": 1.5}, {"name": "Dog", "price": 2.5}]]
    p = consensus_probs(books)
    raw_f, raw_d = 1/1.5, 1/2.5
    s = raw_f + raw_d
    assert p["Fav"] == pytest.approx(raw_f/s)
    assert p["Dog"] == pytest.approx(raw_d/s)
    assert p["Fav"] + p["Dog"] == pytest.approx(1.0)


def test_consensus_averages_across_books():
    books = [
        [{"name": "A", "price": 2.0}, {"name": "B", "price": 2.0}],   # A=0.5
        [{"name": "A", "price": 4.0}, {"name": "B", "price": 4.0/3}],  # A: 0.25/(0.25+0.75)=0.25
    ]
    p = consensus_probs(books)
    assert p["A"] == pytest.approx((0.5 + 0.25) / 2)


# ── unambiguous team matching ───────────────────────────────────────────────

def test_match_unique_substring():
    probs = {"Washington Wizards": 0.02, "Boston Celtics": 0.18}
    assert match_team("Washington", probs) == pytest.approx(0.02)


def test_match_ambiguous_returns_none():
    # "New York" matches both Knicks and Nets -> ambiguous -> skip (None)
    probs = {"New York Knicks": 0.05, "New York Nets": 0.03, "Boston Celtics": 0.2}
    assert match_team("New York", probs) is None


def test_match_no_match_returns_none():
    assert match_team("Nowhere", {"Boston Celtics": 0.2}) is None


# ── gate ────────────────────────────────────────────────────────────────────

def test_gate_keep_deep_longshot():
    # consensus 2% <= max 10% -> keep NO
    assert sports_gate_keep(0.02, max_prob=0.10) is True

def test_gate_skip_not_a_longshot():
    # consensus 30% > max 10% -> skip (not a consensus longshot)
    assert sports_gate_keep(0.30, max_prob=0.10) is False


# ── series -> odds sport key ────────────────────────────────────────────────

def test_kalshi_series_mapping():
    assert kalshi_series_to_odds("KXNBA-27-WAS") == "basketball_nba_championship_winner"
    assert kalshi_series_to_odds("KXMLB-26-WSH") == "baseball_mlb_world_series_winner"
    assert kalshi_series_to_odds("KXHIGHNY-26JUN29-B83.5") is None


# ── client (mocked HTTP + credit cap) ───────────────────────────────────────

def _odds_resp(teams_prices):
    payload = [{"bookmakers": [
        {"key": "bk1", "markets": [{"key": "outrights",
            "outcomes": [{"name": n, "price": p} for n, p in teams_prices]}]},
    ]}]
    r = MagicMock(); r.json = MagicMock(return_value=payload)
    r.raise_for_status = MagicMock(); r.headers = {}
    return r


@pytest.mark.asyncio
async def test_client_championship_probs_and_cache():
    http = MagicMock()
    http.get = AsyncMock(return_value=_odds_resp([("Washington Wizards", 50.0), ("Boston Celtics", 3.0)]))
    c = SportsOddsClient(http=http, api_key="k", cache_ttl_s=9999, max_calls_per_day=10)
    p1 = await c.championship_probs("KXNBA-27-WAS")
    assert p1["Washington Wizards"] == pytest.approx((1/50.0) / (1/50.0 + 1/3.0))
    await c.championship_probs("KXNBA-27-WAS")
    assert http.get.call_count == 1  # cached


@pytest.mark.asyncio
async def test_client_daily_cap_blocks(monkeypatch):
    http = MagicMock()
    http.get = AsyncMock(return_value=_odds_resp([("A", 2.0), ("B", 2.0)]))
    c = SportsOddsClient(http=http, api_key="k", cache_ttl_s=0, max_calls_per_day=1)
    await c.championship_probs("KXNBA-1-A")   # call 1 (uses the 1 allowed)
    await c.championship_probs("KXMLB-1-A")   # call 2 -> capped -> no HTTP, returns {}
    assert http.get.call_count == 1


@pytest.mark.asyncio
async def test_client_missing_key_returns_empty():
    c = SportsOddsClient(http=MagicMock(), api_key=None)
    assert await c.championship_probs("KXNBA-27-WAS") == {}


# ── maker gate dispatch ─────────────────────────────────────────────────────

import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from core.directional.strategies.maker_longshot import MakerLongshotStrategy


class _SportsCfg:
    enabled = True; max_prob = 0.10; require_data = True


class _FakeSports:
    def __init__(self, probs): self._p = probs
    async def championship_probs(self, ticker): return self._p


def _fut_market(team="Washington"):
    m = SimpleNamespace()
    m.ticker = "KXNBA-27-WAS"; m.title = f"Will {team} win the 2027 championship?"
    m.yes_sub_title = team; m.subtitle = ""; m.category = "Sports"
    m.yes_price = 0.04
    m.close_time = datetime.now(timezone.utc) + timedelta(days=200)
    m.to_unified_market_id = lambda: "kalshi:KXNBA-27-WAS"
    return m


def _strat():
    return MakerLongshotStrategy(
        min_structural_score=0.0, max_yes_price=1.0, price_improvement_cents=1,
        skip_categories=[], min_yes_price=0.0, max_days_to_resolution=400,
        sports_cfg=_SportsCfg(),
    )


@pytest.mark.asyncio
async def test_sports_gate_keeps_consensus_longshot():
    s = _strat()
    ctx = {"sports": _FakeSports({"Washington Wizards": 0.02, "Boston Celtics": 0.2})}
    assert await s._apply_sports_gate(_fut_market("Washington"), ctx) is True


@pytest.mark.asyncio
async def test_sports_gate_skips_consensus_favorite():
    s = _strat()
    ctx = {"sports": _FakeSports({"Boston Celtics": 0.30})}
    assert await s._apply_sports_gate(_fut_market("Boston"), ctx) is False


@pytest.mark.asyncio
async def test_sports_gate_skips_no_data_when_require():
    s = _strat()
    assert await s._apply_sports_gate(_fut_market("Washington"), {"sports": _FakeSports({})}) is False


# ── per-game h2h mapping ────────────────────────────────────────────────────

def test_kalshi_game_series_mapping():
    from core.sports_data import kalshi_game_series_to_odds
    assert kalshi_game_series_to_odds("KXMLBGAME-26JUL02-PITPHI-PIT") == "baseball_mlb"
    assert kalshi_game_series_to_odds("KXNBAGAME-26-X") == "basketball_nba"
    assert kalshi_game_series_to_odds("KXMLB-26-X") is None   # futures, not per-game
    assert kalshi_game_series_to_odds("KXHIGHNY-x") is None


def _h2h_resp(events):
    # events: list of (home, away, home_price, away_price)
    payload = [{"home_team": h, "away_team": a, "commence_time": "2026-06-29T22:00:00Z",
                "bookmakers": [{"markets": [{"key": "h2h",
                    "outcomes": [{"name": h, "price": hp}, {"name": a, "price": ap}]}]}]}
               for (h, a, hp, ap) in events]
    from unittest.mock import MagicMock
    r = MagicMock(); r.json = MagicMock(return_value=payload); r.raise_for_status = MagicMock(); r.headers = {}
    return r


@pytest.mark.asyncio
async def test_game_probs_devigs_per_event():
    from unittest.mock import AsyncMock, MagicMock
    from core.sports_data import SportsOddsClient
    http = MagicMock()
    # two games; each event de-vigged independently
    http.get = AsyncMock(return_value=_h2h_resp([
        ("Baltimore Orioles", "Chicago White Sox", 1.74, 2.16),
        ("Pittsburgh Pirates", "Philadelphia Phillies", 2.50, 1.50),
    ]))
    c = SportsOddsClient(http=http, api_key="k", cache_ttl_s=9999, max_calls_per_day=10)
    probs = await c.game_probs("KXMLBGAME-26JUN29-BALCWS-BAL")
    # Orioles de-vig: (1/1.74)/((1/1.74)+(1/2.16)) ~ 0.554
    assert probs["Baltimore Orioles"] == pytest.approx((1/1.74)/((1/1.74)+(1/2.16)), abs=1e-3)
    # each event sums to ~1 independently
    assert probs["Pittsburgh Pirates"] + probs["Philadelphia Phillies"] == pytest.approx(1.0, abs=1e-6)
    # cached: second call no new HTTP
    await c.game_probs("KXMLBGAME-x-y")
    assert http.get.call_count == 1


@pytest.mark.asyncio
async def test_game_probs_unsupported_or_no_key_empty():
    from unittest.mock import MagicMock
    from core.sports_data import SportsOddsClient
    assert await SportsOddsClient(http=MagicMock(), api_key=None).game_probs("KXMLBGAME-x") == {}
    assert await SportsOddsClient(http=MagicMock(), api_key="k").game_probs("KXHIGHNY-x") == {}
