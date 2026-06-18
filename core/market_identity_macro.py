"""
Macro/Politics Market Identity Extractors
==========================================

Extends the v2 structured matcher with identity parsers for:
  - Fed rate decisions (KXFEDDECISION vs PM.US rdc-usfed-fomc-*)
  - Politics / congressional control (KXSENATE / KXHOUSE vs PM.US paccc-*)

CPI / GDP threshold markets are NOT matched here.  The schema mismatch between
PM.US absolute-bucket CPI markets ("CPI == 3.0%") and Kalshi cumulative-threshold
markets ("CPI > 3.0%") means a simple 2-leg MarketPair cannot represent a true
hedge.  A 3-leg structure would be required (PM.US bucket + Kalshi T(x-0.1) YES
+ Kalshi T(x) NO).  Attempting to match them would produce a false hedge — so we
skip them entirely.  This module will grow as genuinely equivalent schemas appear.

Resolution-schema design:
  - FED DECISION: PM.US `rdc-usfed-fomc-{date}-{action}` vs Kalshi
    `KXFEDDECISION-{YYMM}-{code}`.  The action tokens map 1:1 between venues
    when the meeting date, action direction, and magnitude all agree.
  - POLITICS: PM.US `paccc-uss-midterms-2026-11-03-{party}` and
    `paccc-ush-midterms-2026-11-03-{party}` vs Kalshi KXSENATE / KXHOUSE control
    markets (currently 0 open; will open before the 2026 election).  Matching is
    chamber + election-date + party.

LLM equivalence gate (optional):
  When `llm_gate` is injected (an AIAnalyzer-compatible object with a
  `complete(system, user) -> str` coroutine), the matcher calls it as a final
  confirmation on structurally matched candidates.  The gate is:
    - Optional: if None, matching proceeds on structural checks alone.
    - Non-blocking: any exception or parse error degrades to structural result.
    - Default-safe: must not flip a structural match to a miss on error.
    - Config-gated: controlled by caller; the matcher accepts None gracefully.

Public exports:
  MacroPoliticsIdentity   — structured identity for one macro/politics market
  from_kalshi_macro       — parse a KalshiMarket
  from_polymarket_macro   — parse a PM.US Market (polymarket_client.models.Market)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoids circular imports — callers import models directly

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants / lookup tables
# ---------------------------------------------------------------------------

# PM.US Fed slug → canonical action token
# rdc-usfed-fomc-2026-04-29-maintains → action="maintains"
# rdc-usfed-fomc-2026-04-29-cut25bps  → action="cut25bps"
_PMFED_SLUG_RE = re.compile(
    r'^rdc-usfed-fomc-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})-(?P<action>[a-z0-9]+)$'
)

# Kalshi KXFEDDECISION ticker → action decoding
# KXFEDDECISION-{YYMM}-{code}  e.g. KXFEDDECISION-28JAN-H0, -C25, -C26, -H25, -H50
# code: H0=maintain, C25=cut25, C26=cut>25, H25=hike25, H50=hike50, C50=cut50
_KXFEDDECISION_TICKER_RE = re.compile(
    r'^KXFEDDECISION-(?P<yr>\d{2})(?P<mon>[A-Z]{3})-(?P<code>[CHR]\d+)$',
    re.IGNORECASE,
)
_KXFEDDECISION_MONTHS = {
    'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
    'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
}

# PM.US politics slug → chamber + date + party
# paccc-uss-midterms-2026-11-03-rep  → chamber=senate, party=republican
# paccc-ush-midterms-2026-11-03-dem  → chamber=house, party=democrat
# paccc-usho-midterms-2026-11-03-rep (alternate house slug form)
# paccc-usse-midterms-2026-11-03-rep (alternate senate slug form)
_PMPOLITICS_SLUG_RE = re.compile(
    r'^paccc-us(?P<chamber>s|h|se|ho)[-_]midterms?-(?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2})-(?P<party>rep|dem)$'
)
_PMPOLITICS_CHAMBER_MAP = {
    's':  'senate',
    'ss': 'senate',
    'se': 'senate',
    'h':  'house',
    'hh': 'house',
    'ho': 'house',
}

# Kalshi KXSENATE / KXHOUSE control ticker patterns
# KXSENATE-26NOV (event), markets within: KXSENATE-26NOV-R, KXSENATE-26NOV-D
_KXPOLITICS_TICKER_RE = re.compile(
    r'^KX(?P<chamber>SENATE|HOUSE)-(?P<yr>\d{2})(?P<mon>[A-Z]{3})(?:-(?P<party>[RD]))?$',
    re.IGNORECASE,
)

# Map Kalshi action codes to canonical action tokens (matching PM.US slugs)
_KALSHI_CODE_TO_ACTION: dict[str, str] = {
    'H0':  'maintains',   # Hike 0bps = maintain = PM.US "maintains"
    'C25': 'cut25bps',
    'C50': 'cut50bps',
    'C26': 'cutgt25bps',  # Cut > 25bps = PM.US "cutgt25bps"
    'H25': 'hike25bps',
    'H50': 'hike50bps',
    'H26': 'hikegt25bps',  # Hike > 25bps = PM.US "hikegt25bps"
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MacroPoliticsIdentity:
    """
    Structured identity for a macro or politics market.

    Attributes:
        market_type:  "fed_decision" | "politics_control"
        action:       canonical action token (for fed: "maintains","cut25bps",…;
                      for politics: "republican" | "democrat")
        chamber:      for politics only — "senate" | "house" | None
        event_date:   FOMC meeting date or election date
        election_cycle: e.g. 2026 (for politics, guards against stale-cycle mismatches)
    """
    market_type: str
    action: str
    chamber: Optional[str]
    event_date: Optional[date]
    election_cycle: Optional[int]

    def matches(self, other: "MacroPoliticsIdentity") -> tuple[bool, float, str]:
        """
        Compare two identities.  All populated fields must agree.

        Returns (is_match, confidence, reason).
        Confidence=1.0 on full structural agreement; 0.0 on any mismatch.
        """
        if self.market_type != other.market_type:
            return False, 0.0, f"market_type mismatch: {self.market_type!r} vs {other.market_type!r}"

        if self.action != other.action:
            return False, 0.0, f"action mismatch: {self.action!r} vs {other.action!r}"

        if self.chamber and other.chamber and self.chamber != other.chamber:
            return False, 0.0, f"chamber mismatch: {self.chamber!r} vs {other.chamber!r}"

        if self.election_cycle and other.election_cycle:
            if self.election_cycle != other.election_cycle:
                return False, 0.0, (
                    f"election_cycle mismatch: {self.election_cycle} vs {other.election_cycle}"
                )

        if self.event_date and other.event_date:
            # For FOMC: match year+month (Kalshi encodes only month/year in ticker;
            # PM.US has the exact day).  For politics: match year.
            if self.market_type == "fed_decision":
                if (self.event_date.year, self.event_date.month) != (
                    other.event_date.year, other.event_date.month
                ):
                    return False, 0.0, (
                        f"meeting month mismatch: {self.event_date.strftime('%Y-%m')} vs "
                        f"{other.event_date.strftime('%Y-%m')}"
                    )
            else:
                # politics: year must match
                if self.event_date.year != other.event_date.year:
                    return False, 0.0, (
                        f"election year mismatch: {self.event_date.year} vs {other.event_date.year}"
                    )

        reason = (
            f"market_type={self.market_type} action={self.action} "
            f"chamber={self.chamber} "
            f"date={self.event_date or other.event_date}"
        )
        return True, 1.0, reason


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def from_polymarket_macro(market_id: str, question: str, category: str) -> Optional[MacroPoliticsIdentity]:
    """
    Parse structured identity from a PM.US market slug for macro/politics.

    Returns None if the market is not a recognized macro/politics type.
    """
    slug = market_id.lower()

    # Fed Decision
    m = _PMFED_SLUG_RE.match(slug)
    if m:
        try:
            evt_date = date(int(m.group('y')), int(m.group('m')), int(m.group('d')))
        except ValueError:
            evt_date = None
        action = m.group('action').lower()
        return MacroPoliticsIdentity(
            market_type='fed_decision',
            action=action,
            chamber=None,
            event_date=evt_date,
            election_cycle=None,
        )

    # Politics (midterms)
    m = _PMPOLITICS_SLUG_RE.match(slug)
    if m:
        raw_chamber = m.group('chamber').lower()
        chamber = _PMPOLITICS_CHAMBER_MAP.get(raw_chamber)
        if not chamber:
            return None
        party_raw = m.group('party').lower()
        party = 'republican' if party_raw == 'rep' else 'democrat'
        try:
            evt_date = date(int(m.group('y')), int(m.group('m')), int(m.group('d')))
        except ValueError:
            evt_date = None
        cycle = int(m.group('y')) if m.group('y') else None
        return MacroPoliticsIdentity(
            market_type='politics_control',
            action=party,
            chamber=chamber,
            event_date=evt_date,
            election_cycle=cycle,
        )

    return None


def from_kalshi_macro(ticker: str, title: str) -> Optional[MacroPoliticsIdentity]:
    """
    Parse structured identity from a Kalshi ticker for macro/politics.

    Returns None if the ticker is not a recognized macro/politics type.
    """
    ticker_upper = ticker.upper()

    # KXFEDDECISION — categorical Fed action per meeting
    m = _KXFEDDECISION_TICKER_RE.match(ticker_upper)
    if m:
        code = m.group('code').upper()
        action = _KALSHI_CODE_TO_ACTION.get(code)
        if action is None:
            # Unknown action code — cannot safely match
            logger.debug("[macro] Unknown KXFEDDECISION code %r in %r", code, ticker)
            return None
        yr = int(m.group('yr'))
        mon_str = m.group('mon').upper()
        mon = _KXFEDDECISION_MONTHS.get(mon_str)
        if not mon:
            return None
        try:
            # Kalshi encodes only month/year; use day=1 as sentinel
            evt_date = date(2000 + yr, mon, 1)
        except ValueError:
            evt_date = None
        return MacroPoliticsIdentity(
            market_type='fed_decision',
            action=action,
            chamber=None,
            event_date=evt_date,
            election_cycle=None,
        )

    # KXSENATE / KXHOUSE control markets
    m = _KXPOLITICS_TICKER_RE.match(ticker_upper)
    if m:
        chamber_raw = m.group('chamber').lower()
        chamber = 'senate' if 'senate' in chamber_raw else 'house'
        party_code = m.group('party') or ''
        party_code = party_code.upper()
        if party_code == 'R':
            party = 'republican'
        elif party_code == 'D':
            party = 'democrat'
        else:
            # Event-level ticker without party suffix — not a matchable individual market
            return None
        yr = int(m.group('yr'))
        mon_str = m.group('mon').upper()
        mon = _KXFEDDECISION_MONTHS.get(mon_str, 11)
        try:
            evt_date = date(2000 + yr, mon, 1)
        except ValueError:
            evt_date = None
        cycle = 2000 + yr
        return MacroPoliticsIdentity(
            market_type='politics_control',
            action=party,
            chamber=chamber,
            event_date=evt_date,
            election_cycle=cycle,
        )

    return None


# ---------------------------------------------------------------------------
# LLM equivalence gate (optional)
# ---------------------------------------------------------------------------

_LLM_GATE_SYSTEM = (
    "You are a prediction market compliance analyst. "
    "You decide whether two binary market descriptions resolve identically. "
    "Respond ONLY with valid JSON, no markdown, no extra text.\n"
    'Format: {"equivalent": true|false, "yes_maps_to": "yes"|"no", "reason": "..."}\n'
    "Rules:\n"
    "- equivalent=true ONLY if a YES on market A and a YES on market B "
    "  always resolve the same way for every possible real-world outcome.\n"
    "- yes_maps_to: does YES on market B map to YES or NO on market A?\n"
    "- If there is ANY doubt, answer equivalent=false.\n"
    "- reason: ≤2 sentences."
)


async def llm_equivalence_check(
    pm_question: str,
    kalshi_title: str,
    llm_gate,          # AIAnalyzer or any object with async complete(system, user) -> str
) -> tuple[bool, bool, str]:
    """
    Ask the LLM whether two market descriptions resolve identically.

    Returns (equivalent, kalshi_yes_maps_to_pm_yes, reason).
    Degrades gracefully: any exception → (True, True, "LLM gate unavailable").
    Never raises, never blocks matching.
    """
    if llm_gate is None:
        return True, True, "LLM gate not configured"

    user = (
        f"Market A (Polymarket.US): {pm_question}\n"
        f"Market B (Kalshi): {kalshi_title}\n\n"
        "Do they resolve identically? If so, does Kalshi YES map to Polymarket YES or NO?"
    )
    try:
        import json
        text = await llm_gate.complete(_LLM_GATE_SYSTEM, user)
        # Strip markdown fences
        stripped = text.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            stripped = "\n".join(lines).strip()
        data = json.loads(stripped)
        equivalent = bool(data.get("equivalent", True))
        yes_maps = str(data.get("yes_maps_to", "yes")).lower() == "yes"
        reason = str(data.get("reason", "")).strip() or "LLM check passed"
        return equivalent, yes_maps, reason
    except Exception as exc:
        logger.warning("[macro] LLM equivalence gate failed: %s — degrading to structural result", exc)
        return True, True, f"LLM gate error: {exc}"
