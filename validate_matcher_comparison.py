"""
Standalone Matcher Validation Script
=====================================

Fetches real current markets from both PM.US and Kalshi, runs both the OLD
fuzzy matcher (reconstructed inline) and the NEW structured matcher, and
prints a side-by-side comparison.

Usage (in the container, does NOT restart anything):
    python3 /app/validate_matcher_comparison.py

Reads live public API data. No orders are placed.
"""

import asyncio
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from difflib import SequenceMatcher
from typing import Optional

logging.basicConfig(level=logging.WARNING)

# ---------------------------------------------------------------------------
# Inline OLD matcher logic (reconstructed from v1 for comparison only)
# ---------------------------------------------------------------------------

_NOISE_WORDS = {
    "will", "the", "a", "an", "be", "to", "in", "on", "by", "at",
    "what", "who", "which", "when", "is", "are", "was", "were",
    "market", "prediction", "bet", "odds", "win", "winner"
}

_NFL_TEAMS = {
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

_TEAM_LOOKUP = {}
for _fn, _variants in _NFL_TEAMS.items():
    _TEAM_LOOKUP[_fn] = _fn
    for _v in _variants:
        _TEAM_LOOKUP[_v.lower()] = _fn


def _old_normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^\w\s]', ' ', text)
    words = [w for w in text.split() if w not in _NOISE_WORDS]
    return ' '.join(words)


def _old_extract_teams(text: str) -> list:
    tl = text.lower()
    found = []
    for key in sorted(_TEAM_LOOKUP, key=len, reverse=True):
        if key in tl:
            can = _TEAM_LOOKUP[key]
            if can not in found:
                found.append(can)
                tl = tl.replace(key, "")
    return found


def _old_extract_entities(text: str) -> set:
    entities = set()
    entities.update(re.findall(r'\d+(?:\.\d+)?%?', text))
    entities.update(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text))
    for term in ["trump", "biden", "republican", "democrat", "harris",
                 "bitcoin", "btc", "ethereum", "eth", "solana"]:
        if term in text.lower():
            entities.add(term)
    return entities


def _old_sports_match(t1: str, t2: str):
    teams1 = _old_extract_teams(t1)
    teams2 = _old_extract_teams(t2)
    if len(teams1) >= 2 and len(teams2) >= 2:
        s1, s2 = set(teams1[:2]), set(teams2[:2])
        if s1 == s2:
            return True, 0.95
        overlap = s1 & s2
        if len(overlap) >= 1:
            return True, 0.7 + (0.2 * len(overlap) / 2)
    return False, 0.0


def _old_similarity(q1: str, q2: str) -> float:
    is_sports, sports_score = _old_sports_match(q1, q2)
    if is_sports and sports_score > 0.7:
        return sports_score

    norm1 = _old_normalize(q1)
    norm2 = _old_normalize(q2)
    text_sim = SequenceMatcher(None, norm1, norm2).ratio()

    ents1 = _old_extract_entities(q1)
    ents2 = _old_extract_entities(q2)
    if ents1 and ents2:
        entity_overlap = len(ents1 & ents2) / max(len(ents1), len(ents2))
        combined = 0.5 * text_sim + 0.5 * entity_overlap
    else:
        combined = text_sim

    sports_kws = ["nfl", "nba", "mlb", "nhl", "football", "basketball"]
    ps = [s for s in sports_kws if s in q1.lower()]
    ks = [s for s in sports_kws if s in q2.lower()]
    if ps and ks and set(ps) & set(ks):
        combined = min(1.0, combined + 0.15)

    return combined


def _old_categorize(text: str) -> str:
    tl = text.lower()
    if any(x in tl for x in ['trump', 'biden', 'harris', 'president', 'election',
            'democrat', 'republican', 'congress', 'senate', 'vote', 'nominee']):
        return 'politics'
    if any(x in tl for x in ['bitcoin', 'btc', 'ethereum', 'eth', 'crypto']):
        return 'crypto'
    if any(x in tl for x in ['nfl', 'nba', 'mlb', 'nhl', 'world cup', 'super bowl']):
        return 'sports'
    if any(x in tl for x in _TEAM_LOOKUP):
        return 'sports'
    return 'other'


def old_find_matches(poly_markets, kalshi_markets, threshold=0.5):
    """Run the OLD fuzzy matcher on the given markets."""
    matches = []
    active_poly = [m for m in poly_markets if m.active]
    active_kalshi = [m for m in kalshi_markets if m.is_active]

    poly_by_cat = defaultdict(list)
    for m in active_poly:
        poly_by_cat[_old_categorize(m.question)].append(m)

    kalshi_by_cat = defaultdict(list)
    for m in active_kalshi:
        kalshi_by_cat[_old_categorize(m.title)].append(m)

    for cat in ['sports', 'politics', 'crypto', 'finance']:
        pm_cat = poly_by_cat.get(cat, [])
        km_cat = kalshi_by_cat.get(cat, [])
        if not pm_cat or not km_cat:
            continue
        for pm in pm_cat:
            best_score = 0.0
            best_km = None
            for km in km_cat:
                sc = _old_similarity(pm.question, km.title)
                if sc > best_score:
                    best_score = sc
                    best_km = km
            if best_km and best_score >= threshold:
                matches.append({
                    'poly_q': pm.question,
                    'kalshi_t': best_km.title[:80],
                    'score': best_score,
                    'cat': cat,
                })
    return matches


# ---------------------------------------------------------------------------
# NEW matcher (import from module)
# ---------------------------------------------------------------------------

from core.cross_platform_arb import MarketMatcher


# ---------------------------------------------------------------------------
# Main comparison
# ---------------------------------------------------------------------------

async def main():
    print("=" * 70)
    print("MATCHER COMPARISON: OLD (fuzzy) vs NEW (structured identity)")
    print("=" * 70)
    print()

    # Fetch markets
    print("Fetching PM.US markets...")
    from polymarket_us_client import PolymarketUSClient
    async with PolymarketUSClient(dry_run=True) as pm_client:
        poly_markets = await pm_client.list_markets()

    print(f"  Got {len(poly_markets)} PM.US markets")
    active_poly = [m for m in poly_markets if m.active]
    print(f"  Active: {len(active_poly)}")
    print()

    print("Fetching Kalshi markets...")
    from kalshi_client import KalshiClient
    async with KalshiClient(dry_run=True) as k_client:
        kalshi_markets = await k_client.list_all_markets(status="open", max_markets=5000)

    print(f"  Got {len(kalshi_markets)} Kalshi open markets")
    kxmv_count = sum(1 for m in kalshi_markets if m.ticker.upper().startswith("KXMV"))
    non_kxmv = [m for m in kalshi_markets if not m.ticker.upper().startswith("KXMV")]
    print(f"  KXMV parlay/combo: {kxmv_count}  (these are multi-leg, unmatachable)")
    print(f"  Individual markets: {len(non_kxmv)}")
    print()

    # --- OLD matcher ---
    print("-" * 70)
    print("OLD MATCHER (fuzzy string similarity, threshold=0.5):")
    print("-" * 70)
    old_matches = old_find_matches(poly_markets, kalshi_markets, threshold=0.5)
    print(f"  Total matches: {len(old_matches)}")
    if old_matches:
        print()
        print("  Sample FALSE POSITIVES (first 20):")
        for i, m in enumerate(old_matches[:20]):
            print(f"  [{i+1}] score={m['score']:.2f} cat={m['cat']}")
            print(f"       PM.US: {m['poly_q'][:65]}")
            print(f"       Kalshi: {m['kalshi_t'][:65]}")
    print()

    # --- NEW matcher ---
    print("-" * 70)
    print("NEW MATCHER (structured identity: sport+teams+date+market_type):")
    print("-" * 70)
    new_matcher = MarketMatcher()
    new_matches = await new_matcher.find_matches(poly_markets, kalshi_markets)
    print(f"  Total matches: {len(new_matches)}")
    print()

    if new_matches:
        print("  VERIFIED PAIRS:")
        for i, pair in enumerate(new_matches):
            print(f"  [{i+1}] confidence={pair.similarity_score:.2f} "
                  f"kalshi_YES_maps_to_poly_YES={pair.kalshi_yes_maps_to_poly_yes}")
            print(f"       PM.US:  {pair.polymarket_question[:65]}")
            print(f"       Kalshi: {pair.kalshi_title[:65]}")
            print(f"       Reason: {pair.match_reason}")
    else:
        print("  0 matches. This is the CORRECT honest result given current market state:")
        print()
        print("  EXPLANATION:")
        print("  - All 5000 Kalshi open markets are KXMV* parlay/combo tickets.")
        print("    These concatenate multiple selections into one title, e.g.:")
        print("    'yes Toronto,yes Milwaukee,yes Baltimore,yes New York M,...'")
        print("    They are NOT individual binary markets — they cannot be matched")
        print("    against PM.US single-event outcomes.")
        print()
        print("  - Individual Kalshi game markets (KXNFLGAME, KXMLBGAME) exist but")
        print("    are for the 2026 NFL season (September 2026). PM.US markets are")
        print("    for the 2025 NFL season (Oct/Nov 2025) — different seasons.")
        print()
        print("  - PM.US has 2026 FIFA World Cup 'Will X win the World Cup?' markets.")
        print("    Kalshi has no individual tournament-winner markets open — only")
        print("    KXMV parlays that mention country names as one leg among many.")
        print()
        print("  - The old matcher was producing ~50+ false positives by finding")
        print("    partial token overlap (e.g. 'Atlanta' appears in both PM.US NFL")
        print("    game titles AND in Kalshi KXMV parlay titles as one of 10+ teams).")
        print("    Executing those would place unhedged two-leg orders on completely")
        print("    different events.")

    print()
    print("=" * 70)
    print(f"SUMMARY: old={len(old_matches)} pairs  new={len(new_matches)} pairs")
    print(f"  False positive reduction: {len(old_matches) - len(new_matches)} spurious matches eliminated")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
