"""
Tests for macro/politics structured identity matching.

Covers:
  1. True political match (same chamber/cycle/party, YES mapping correct)
  2. True Fed-decision match (same meeting month/action, YES==YES)
  3. CPI threshold alignment: Kalshi threshold markets DO NOT match PM.US
     absolute-bucket CPI markets (schema mismatch — would be a false hedge)
  4. Reject same-topic-different-resolution (CPI bucket vs threshold)
  5. Reject different election cycle (2026 slug vs 2028 Kalshi)
  6. Reject different action (cut25 vs maintains)
  7. Reject different chamber (senate vs house)
  8. LLM gate fallback: disabled gate still produces match
  9. LLM gate rejection: gate returning equivalent=False kills structural match
 10. Macro markets flow through find_matches() end-to-end
 11. CPI KXCPIYOY tickers are NOT matched to PM.US cpic-* slugs
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.cross_platform_arb import (
    MarketMatcher,
    MarketPair,
    _MarketIdentity,
)
from core.market_identity_macro import (
    MacroPoliticsIdentity,
    from_polymarket_macro,
    from_kalshi_macro,
    llm_equivalence_check,
)


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

@dataclass
class _FakePM:
    """Minimal PM.US market stub."""
    market_id: str
    question: str
    active: bool = True
    category: str = "politics"
    description: str = ""
    closed: bool = False
    volume_24h: float = 0.0
    liquidity: float = 0.0
    yes_token_id: str = ""
    no_token_id: str = ""
    condition_id: str = ""


@dataclass
class _FakeKalshi:
    """Minimal Kalshi market stub."""
    ticker: str
    title: str
    status: str = "open"
    subtitle: str = ""
    event_ticker: str = ""
    series_ticker: str = ""
    yes_price: float = 0.5
    no_price: float = 0.5
    volume: int = 0
    open_interest: int = 0
    close_time: Optional[datetime] = None
    expiration_time: Optional[datetime] = None
    result: Optional[str] = None
    category: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in ("open", "active")


def run(coro):
    """Run a coroutine synchronously using a fresh event loop each time.

    Using asyncio.new_event_loop() avoids the closed-loop error that occurs
    when pytest-asyncio's async fixture teardown closes the shared loop before
    our synchronous helper can use it.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# 1. MacroPoliticsIdentity — basic parsing
# ---------------------------------------------------------------------------

class TestFromPolymarketMacro:
    """from_polymarket_macro parser."""

    def test_parses_senate_republican(self):
        mid = from_polymarket_macro(
            "paccc-uss-midterms-2026-11-03-rep",
            "Which party will win the U.S. Senate?",
            "politics",
        )
        assert mid is not None
        assert mid.market_type == "politics_control"
        assert mid.action == "republican"
        assert mid.chamber == "senate"
        assert mid.election_cycle == 2026

    def test_parses_senate_democrat(self):
        mid = from_polymarket_macro(
            "paccc-uss-midterms-2026-11-03-dem",
            "Which party will win the U.S. Senate?",
            "politics",
        )
        assert mid is not None
        assert mid.action == "democrat"
        assert mid.chamber == "senate"

    def test_parses_house_republican(self):
        mid = from_polymarket_macro(
            "paccc-ush-midterms-2026-11-03-rep",
            "Which party will win the U.S. House?",
            "politics",
        )
        assert mid is not None
        assert mid.chamber == "house"
        assert mid.action == "republican"

    def test_alternate_house_slug_form(self):
        mid = from_polymarket_macro(
            "paccc-usho-midterms-2026-11-03-rep",
            "U.S House Midterm Winner",
            "politics",
        )
        assert mid is not None
        assert mid.chamber == "house"
        assert mid.action == "republican"

    def test_alternate_senate_slug_form(self):
        mid = from_polymarket_macro(
            "paccc-usse-midterms-2026-11-03-dem",
            "U.S Senate Midterm Winner",
            "politics",
        )
        assert mid is not None
        assert mid.chamber == "senate"
        assert mid.action == "democrat"

    def test_parses_fed_maintains(self):
        mid = from_polymarket_macro(
            "rdc-usfed-fomc-2026-04-29-maintains",
            "Fed Decision in April",
            "macro",
        )
        assert mid is not None
        assert mid.market_type == "fed_decision"
        assert mid.action == "maintains"
        assert mid.event_date == date(2026, 4, 29)

    def test_parses_fed_cut25bps(self):
        mid = from_polymarket_macro(
            "rdc-usfed-fomc-2026-04-29-cut25bps",
            "Fed Decision in April",
            "macro",
        )
        assert mid is not None
        assert mid.action == "cut25bps"
        assert mid.event_date.month == 4

    def test_parses_fed_cutgt25bps(self):
        mid = from_polymarket_macro(
            "rdc-usfed-fomc-2026-04-29-cutgt25bps",
            "Fed Decision in April",
            "macro",
        )
        assert mid is not None
        assert mid.action == "cutgt25bps"

    def test_sports_slug_returns_none(self):
        assert from_polymarket_macro(
            "aec-nfl-atl-ne-2025-11-02",
            "Will Atlanta win?",
            "sports",
        ) is None

    def test_cpi_bucket_slug_returns_none(self):
        """CPI bucket slugs must NOT be parsed — schema mismatch with Kalshi."""
        assert from_polymarket_macro(
            "cpic-uscpi-apr2026yoy-2026-05-12-3pt0pct",
            "CPI year-over-year in April",
            "macro",
        ) is None

    def test_unknown_slug_returns_none(self):
        assert from_polymarket_macro("random-slug-xyz", "Some market", "") is None


class TestFromKalshiMacro:
    """from_kalshi_macro parser."""

    def test_parses_kxfeddecision_maintain(self):
        mid = from_kalshi_macro("KXFEDDECISION-26APR-H0", "Will Fed hold rates?")
        assert mid is not None
        assert mid.market_type == "fed_decision"
        assert mid.action == "maintains"
        assert mid.event_date.year == 2026
        assert mid.event_date.month == 4

    def test_parses_kxfeddecision_cut25(self):
        mid = from_kalshi_macro("KXFEDDECISION-26APR-C25", "Will Fed cut 25bps?")
        assert mid is not None
        assert mid.action == "cut25bps"

    def test_parses_kxfeddecision_cutgt25(self):
        mid = from_kalshi_macro("KXFEDDECISION-26APR-C26", "Will Fed cut >25bps?")
        assert mid is not None
        assert mid.action == "cutgt25bps"

    def test_parses_kxfeddecision_hike25(self):
        mid = from_kalshi_macro("KXFEDDECISION-26APR-H25", "Will Fed hike 25bps?")
        assert mid is not None
        assert mid.action == "hike25bps"

    def test_unknown_code_returns_none(self):
        """Unknown action code must not produce a match."""
        result = from_kalshi_macro("KXFEDDECISION-26APR-X99", "Unknown code")
        assert result is None

    def test_parses_kxsenate_republican(self):
        mid = from_kalshi_macro("KXSENATE-26NOV-R", "Will Republicans control the Senate?")
        assert mid is not None
        assert mid.market_type == "politics_control"
        assert mid.action == "republican"
        assert mid.chamber == "senate"
        assert mid.election_cycle == 2026

    def test_parses_kxhouse_democrat(self):
        mid = from_kalshi_macro("KXHOUSE-26NOV-D", "Will Democrats control the House?")
        assert mid is not None
        assert mid.action == "democrat"
        assert mid.chamber == "house"

    def test_kxsenate_event_level_no_party_returns_none(self):
        """Event-level ticker KXSENATE-26NOV without party suffix is not matchable."""
        result = from_kalshi_macro("KXSENATE-26NOV", "Senate control")
        assert result is None

    def test_kxcpiyoy_threshold_returns_none(self):
        """KXCPIYOY threshold markets must NOT be parsed — schema mismatch."""
        result = from_kalshi_macro("KXCPIYOY-26NOV-T3.0", "Will CPI be above 3.0%?")
        assert result is None

    def test_kxmv_parlay_returns_none(self):
        result = from_kalshi_macro("KXMVESPORTSMULTIGAME-S2026-ABC", "Parlay market")
        assert result is None

    def test_sports_ticker_returns_none(self):
        result = from_kalshi_macro("KXNFLGAME-26SEP13ATLPIT-ATL", "NFL game")
        assert result is None


# ---------------------------------------------------------------------------
# 2. MacroPoliticsIdentity.matches() — equivalence logic
# ---------------------------------------------------------------------------

class TestMacroPoliticsIdentityMatches:

    def _fed(self, action: str, yr: int = 2026, mon: int = 4) -> MacroPoliticsIdentity:
        return MacroPoliticsIdentity(
            market_type="fed_decision",
            action=action,
            chamber=None,
            event_date=date(yr, mon, 1),
            election_cycle=None,
        )

    def _politics(self, chamber: str, party: str, cycle: int = 2026) -> MacroPoliticsIdentity:
        return MacroPoliticsIdentity(
            market_type="politics_control",
            action=party,
            chamber=chamber,
            event_date=date(cycle, 11, 1),
            election_cycle=cycle,
        )

    def test_fed_match_same_action_same_month(self):
        a = self._fed("maintains", 2026, 4)
        b = self._fed("maintains", 2026, 4)
        ok, conf, reason = a.matches(b)
        assert ok
        assert conf == 1.0

    def test_fed_match_same_month_different_day(self):
        """Kalshi only encodes month; PM.US has exact day — still matches."""
        a = MacroPoliticsIdentity("fed_decision", "maintains", None, date(2026, 4, 29), None)
        b = MacroPoliticsIdentity("fed_decision", "maintains", None, date(2026, 4, 1), None)
        ok, _, _ = a.matches(b)
        assert ok

    def test_fed_reject_different_action(self):
        a = self._fed("maintains")
        b = self._fed("cut25bps")
        ok, conf, reason = a.matches(b)
        assert not ok
        assert "action mismatch" in reason

    def test_fed_reject_different_month(self):
        a = self._fed("cut25bps", 2026, 4)
        b = self._fed("cut25bps", 2026, 6)
        ok, _, reason = a.matches(b)
        assert not ok
        assert "meeting month mismatch" in reason

    def test_fed_reject_different_type(self):
        a = self._fed("maintains")
        b = self._politics("senate", "republican")
        ok, _, reason = a.matches(b)
        assert not ok
        assert "market_type mismatch" in reason

    def test_politics_match_same_chamber_party_cycle(self):
        a = self._politics("senate", "republican", 2026)
        b = self._politics("senate", "republican", 2026)
        ok, conf, _ = a.matches(b)
        assert ok
        assert conf == 1.0

    def test_politics_reject_different_party(self):
        a = self._politics("senate", "republican")
        b = self._politics("senate", "democrat")
        ok, _, reason = a.matches(b)
        assert not ok
        assert "action mismatch" in reason

    def test_politics_reject_different_chamber(self):
        a = self._politics("senate", "republican")
        b = self._politics("house", "republican")
        ok, _, reason = a.matches(b)
        assert not ok
        assert "chamber mismatch" in reason

    def test_politics_reject_different_cycle(self):
        a = self._politics("senate", "republican", 2026)
        b = self._politics("senate", "republican", 2028)
        ok, _, reason = a.matches(b)
        assert not ok
        assert "election_cycle mismatch" in reason


# ---------------------------------------------------------------------------
# 3. _MarketIdentity integration — macro/politics flow through the outer class
# ---------------------------------------------------------------------------

class TestMarketIdentityMacroIntegration:

    def test_from_polymarket_returns_macro_sport(self):
        mid = _MarketIdentity.from_polymarket(
            "rdc-usfed-fomc-2026-04-29-maintains",
            "Fed Decision in April",
            "macro",
        )
        assert mid is not None
        assert mid.sport == "macro"
        assert mid.market_type == "fed_decision"
        assert mid.macro_id is not None
        assert mid.macro_id.action == "maintains"

    def test_from_kalshi_returns_macro_sport(self):
        mid = _MarketIdentity.from_kalshi(
            "KXFEDDECISION-26APR-H0",
            "Will the Federal Reserve hold rates?",
        )
        assert mid is not None
        assert mid.sport == "macro"
        assert mid.macro_id is not None
        assert mid.macro_id.action == "maintains"

    def test_from_polymarket_returns_politics_sport(self):
        mid = _MarketIdentity.from_polymarket(
            "paccc-uss-midterms-2026-11-03-rep",
            "Which party will win the U.S. Senate?",
            "politics",
        )
        assert mid is not None
        assert mid.sport == "politics"
        assert mid.macro_id is not None
        assert mid.macro_id.chamber == "senate"

    def test_macro_identity_matches_via_outer_class(self):
        poly_id = _MarketIdentity.from_polymarket(
            "rdc-usfed-fomc-2026-04-29-maintains",
            "Fed Decision in April",
            "macro",
        )
        kalshi_id = _MarketIdentity.from_kalshi(
            "KXFEDDECISION-26APR-H0",
            "Will the Federal Reserve hold rates at their April 2026 meeting?",
        )
        assert poly_id is not None
        assert kalshi_id is not None
        ok, conf, reason = poly_id.matches(kalshi_id)
        assert ok, f"Expected match, got reason: {reason}"
        assert conf == 1.0

    def test_cpi_bucket_vs_threshold_no_match(self):
        """PM.US CPI bucket slug must produce None from from_polymarket → no match."""
        poly_id = _MarketIdentity.from_polymarket(
            "cpic-uscpi-apr2026yoy-2026-05-12-3pt0pct",
            "CPI year-over-year in April",
            "macro",
        )
        assert poly_id is None, (
            "CPI absolute-bucket slugs must not parse (schema mismatch with Kalshi thresholds)"
        )

    def test_kxcpiyoy_threshold_no_match(self):
        """Kalshi KXCPIYOY threshold tickers must produce None from from_kalshi."""
        kalshi_id = _MarketIdentity.from_kalshi(
            "KXCPIYOY-26NOV-T3.0",
            "Will the rate of CPI inflation be above 3.0% for the year ending in November 2026?",
        )
        assert kalshi_id is None, (
            "KXCPIYOY threshold markets must not parse (schema mismatch with PM.US buckets)"
        )

    def test_politics_identity_matches_via_outer_class(self):
        poly_id = _MarketIdentity.from_polymarket(
            "paccc-uss-midterms-2026-11-03-rep",
            "Which party will win the U.S. Senate?",
            "politics",
        )
        kalshi_id = _MarketIdentity.from_kalshi(
            "KXSENATE-26NOV-R",
            "Will Republicans control the Senate after the 2026 election?",
        )
        assert poly_id is not None
        assert kalshi_id is not None
        ok, conf, reason = poly_id.matches(kalshi_id)
        assert ok, f"Expected match, got reason: {reason}"

    def test_fed_reject_cross_action(self):
        poly_id = _MarketIdentity.from_polymarket(
            "rdc-usfed-fomc-2026-04-29-maintains",
            "Fed Decision in April",
            "macro",
        )
        kalshi_id = _MarketIdentity.from_kalshi(
            "KXFEDDECISION-26APR-C25",
            "Will the Federal Reserve cut rates 25bps?",
        )
        assert poly_id is not None
        assert kalshi_id is not None
        ok, _, reason = poly_id.matches(kalshi_id)
        assert not ok
        assert "action mismatch" in reason

    def test_politics_reject_wrong_chamber(self):
        poly_id = _MarketIdentity.from_polymarket(
            "paccc-uss-midterms-2026-11-03-rep",
            "Which party will win the U.S. Senate?",
            "politics",
        )
        kalshi_id = _MarketIdentity.from_kalshi(
            "KXHOUSE-26NOV-R",
            "Will Republicans control the House?",
        )
        assert poly_id is not None
        assert kalshi_id is not None
        ok, _, reason = poly_id.matches(kalshi_id)
        assert not ok


# ---------------------------------------------------------------------------
# 4. LLM gate behaviour
# ---------------------------------------------------------------------------

class TestLLMGate:

    def test_llm_gate_none_returns_structural_accept(self):
        ok, yes_map, reason = run(llm_equivalence_check("Q1", "Q2", None))
        assert ok
        assert yes_map
        assert "not configured" in reason.lower()

    def test_llm_gate_disabled_on_analyzer_exception(self):
        class BrokenLLM:
            async def complete(self, system, user):
                raise RuntimeError("no connection")

        ok, yes_map, reason = run(llm_equivalence_check("Q1", "Q2", BrokenLLM()))
        # Must degrade to accept
        assert ok
        assert "error" in reason.lower() or "gate" in reason.lower()

    def test_llm_gate_accepts_on_equivalent_true(self):
        class GoodLLM:
            async def complete(self, system, user):
                return '{"equivalent": true, "yes_maps_to": "yes", "reason": "Same binary event."}'

        ok, yes_map, reason = run(llm_equivalence_check("Fed holds rates?", "Will Fed keep rates?", GoodLLM()))
        assert ok
        assert yes_map

    def test_llm_gate_rejects_on_equivalent_false(self):
        class RejectingLLM:
            async def complete(self, system, user):
                return '{"equivalent": false, "yes_maps_to": "yes", "reason": "Different schemas."}'

        ok, yes_map, reason = run(llm_equivalence_check("CPI exact 3.0%?", "CPI above 3.0%?", RejectingLLM()))
        assert not ok
        assert "Different schemas" in reason

    def test_llm_gate_handles_markdown_fence(self):
        class FencedLLM:
            async def complete(self, system, user):
                return '```json\n{"equivalent": true, "yes_maps_to": "no", "reason": "YES on B = NO on A."}\n```'

        ok, yes_map, reason = run(llm_equivalence_check("Q1", "Q2", FencedLLM()))
        assert ok
        assert not yes_map  # yes_maps_to: "no"

    def test_llm_gate_handles_malformed_json(self):
        class MalformedLLM:
            async def complete(self, system, user):
                return "This is not JSON"

        ok, yes_map, reason = run(llm_equivalence_check("Q1", "Q2", MalformedLLM()))
        # JSON parse error → degrade to accept
        assert ok


# ---------------------------------------------------------------------------
# 5. End-to-end find_matches() with fixture macro/politics markets
# ---------------------------------------------------------------------------

class TestFindMatchesMacro:
    """
    Demonstrates that when Fed-decision markets are simultaneously open on
    both venues, the matcher correctly pairs them.  Uses fixture markets
    (real slugs / real ticker formats from actual API data).
    """

    def _pm_fed(self, action: str) -> _FakePM:
        return _FakePM(
            market_id=f"rdc-usfed-fomc-2026-04-29-{action}",
            question="Fed Decision in April",
            category="macro",
        )

    def _kalshi_fed(self, code: str) -> _FakeKalshi:
        code_to_title = {
            "H0":  "Will the Federal Reserve Hike rates by 0bps at their April 2026 meeting?",
            "C25": "Will the Federal Reserve Cut rates by 25bps at their April 2026 meeting?",
            "C26": "Will the Federal Reserve Cut rates by >25bps at their April 2026 meeting?",
            "H25": "Will the Federal Reserve Hike rates by 25bps at their April 2026 meeting?",
        }
        return _FakeKalshi(
            ticker=f"KXFEDDECISION-26APR-{code}",
            title=code_to_title.get(code, f"Fed action {code}"),
            status="active",
        )

    def test_fed_maintains_paired(self):
        pm_markets = [self._pm_fed("maintains")]
        k_markets = [self._kalshi_fed("H0"), self._kalshi_fed("C25")]
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches(pm_markets, k_markets))
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "KXFEDDECISION-26APR-H0"
        assert "fed_decision" in pairs[0].match_reason
        assert pairs[0].kalshi_yes_maps_to_poly_yes is True

    def test_fed_cut25bps_paired(self):
        pm_markets = [self._pm_fed("cut25bps")]
        k_markets = [self._kalshi_fed("H0"), self._kalshi_fed("C25")]
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches(pm_markets, k_markets))
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "KXFEDDECISION-26APR-C25"

    def test_fed_no_match_when_action_absent(self):
        """PM.US cut25bps should not pair with Kalshi H0 (maintain)."""
        pm_markets = [self._pm_fed("cut25bps")]
        k_markets = [self._kalshi_fed("H0")]  # Only maintain available
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches(pm_markets, k_markets))
        assert len(pairs) == 0

    def test_fed_multiple_pm_markets_each_pair_correctly(self):
        """Each PM.US Fed action slug should pair with its exact Kalshi counterpart."""
        pm_markets = [
            self._pm_fed("maintains"),
            self._pm_fed("cut25bps"),
            self._pm_fed("cutgt25bps"),
        ]
        k_markets = [
            self._kalshi_fed("H0"),
            self._kalshi_fed("C25"),
            self._kalshi_fed("C26"),
        ]
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches(pm_markets, k_markets))
        assert len(pairs) == 3
        tickers = {p.kalshi_ticker for p in pairs}
        assert "KXFEDDECISION-26APR-H0" in tickers
        assert "KXFEDDECISION-26APR-C25" in tickers
        assert "KXFEDDECISION-26APR-C26" in tickers

    def test_politics_senate_republican_paired(self):
        pm = _FakePM(
            market_id="paccc-uss-midterms-2026-11-03-rep",
            question="Which party will win the U.S. Senate?",
            category="politics",
        )
        kl = _FakeKalshi(
            ticker="KXSENATE-26NOV-R",
            title="Will Republicans control the Senate after the 2026 election?",
        )
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm], [kl]))
        assert len(pairs) == 1
        assert pairs[0].category == "politics"
        assert "politics_control" in pairs[0].match_reason

    def test_politics_reject_wrong_party(self):
        pm = _FakePM(
            market_id="paccc-uss-midterms-2026-11-03-rep",
            question="Which party will win the U.S. Senate?",
            category="politics",
        )
        kl = _FakeKalshi(
            ticker="KXSENATE-26NOV-D",
            title="Will Democrats control the Senate after the 2026 election?",
        )
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm], [kl]))
        assert len(pairs) == 0

    def test_cpi_bucket_vs_threshold_no_match(self):
        """PM.US CPI bucket slugs and Kalshi KXCPIYOY threshold tickers must never pair."""
        pm = _FakePM(
            market_id="cpic-uscpi-apr2026yoy-2026-05-12-3pt0pct",
            question="CPI year-over-year in April",
            category="macro",
        )
        kl = _FakeKalshi(
            ticker="KXCPIYOY-26NOV-T3.0",
            title="Will the rate of CPI inflation be above 3.0% for the year ending in November 2026?",
        )
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm], [kl]))
        assert len(pairs) == 0, (
            "CPI bucket vs threshold should never produce a match — "
            "they are NOT the same binary event."
        )

    def test_sports_markets_still_work_alongside_macro(self):
        """Adding macro markets must not break existing sports matching."""
        pm_sports = _FakePM(
            market_id="aec-nfl-atl-ne-2025-09-13",
            question="Will Atlanta win?",
            category="sports",
        )
        pm_fed = self._pm_fed("maintains")
        kl_sports = _FakeKalshi(
            ticker="KXNFLGAME-25SEP13ATLNE-ATL",
            title="Will Atlanta win the New England vs Atlanta Pro Football game?",
        )
        kl_fed = self._kalshi_fed("H0")
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm_sports, pm_fed], [kl_sports, kl_fed]))
        categories = {p.category for p in pairs}
        assert "nfl" in categories
        assert "macro" in categories

    def test_llm_gate_rejection_skips_pair(self):
        """When LLM gate rejects a structural match, the pair is not emitted."""

        class RejectAllLLM:
            async def complete(self, system, user):
                return '{"equivalent": false, "yes_maps_to": "yes", "reason": "LLM says no."}'

        pm = _FakePM(
            market_id="rdc-usfed-fomc-2026-04-29-maintains",
            question="Fed Decision in April",
            category="macro",
        )
        kl = _FakeKalshi(
            ticker="KXFEDDECISION-26APR-H0",
            title="Will the Federal Reserve hold rates?",
        )
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm], [kl], llm_gate=RejectAllLLM()))
        assert len(pairs) == 0

    def test_llm_gate_error_accepts_pair(self):
        """When LLM gate throws, structural match is preserved (fail-safe)."""

        class ErrorLLM:
            async def complete(self, system, user):
                raise ConnectionError("LLM offline")

        pm = _FakePM(
            market_id="rdc-usfed-fomc-2026-04-29-maintains",
            question="Fed Decision in April",
            category="macro",
        )
        kl = _FakeKalshi(
            ticker="KXFEDDECISION-26APR-H0",
            title="Will the Federal Reserve hold rates?",
        )
        matcher = MarketMatcher()
        pairs = run(matcher.find_matches([pm], [kl], llm_gate=ErrorLLM()))
        assert len(pairs) == 1

    def test_get_cached_pairs_includes_macro(self):
        pm = _FakePM(
            market_id="rdc-usfed-fomc-2026-04-29-maintains",
            question="Fed Decision in April",
            category="macro",
        )
        kl = _FakeKalshi(
            ticker="KXFEDDECISION-26APR-H0",
            title="Will the Federal Reserve hold rates?",
        )
        matcher = MarketMatcher()
        run(matcher.find_matches([pm], [kl]))
        cached = matcher.get_cached_pairs()
        assert len(cached) == 1
        assert cached[0].category == "macro"
