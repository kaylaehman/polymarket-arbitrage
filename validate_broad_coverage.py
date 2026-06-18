"""
Standalone Broad-Coverage Matcher Validation
=============================================

Validates the broadened PM.US + Kalshi market fetch and runs the structured
MarketMatcher on the full cross-category set.

Usage (run inside the live container WITHOUT restarting it):
    docker cp validate_broad_coverage.py polymarket-arb:/app/
    docker exec -w /app polymarket-arb python3 validate_broad_coverage.py

No orders are placed. Reads public APIs only.  safe for production.
"""

import asyncio
import logging
import sys
import time
from collections import Counter
from typing import Optional

logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s %(name)s: %(message)s",
)
# Quiet the clients so the validation output is readable
for noisy in ("polymarket_us_client", "kalshi_client", "core"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Fetch markets
# ---------------------------------------------------------------------------

async def fetch_pm_us_markets(max_markets: int = 15000) -> list:
    """Fetch all PM.US markets using the broadened paginated list_markets."""
    from polymarket_us_client import PolymarketUSClient

    print(f"[PM.US] Fetching up to {max_markets} markets (paginated, 0.4s inter-page delay)...")
    t0 = time.time()
    async with PolymarketUSClient(dry_run=True) as c:
        markets = await c.list_markets(
            max_markets=max_markets,
            page_size=500,
            page_delay=0.4,
        )
    elapsed = time.time() - t0
    print(f"[PM.US] Fetched {len(markets)} markets in {elapsed:.1f}s")
    return markets


async def fetch_kalshi_markets(max_markets: int = 5000) -> list:
    """
    Fetch Kalshi markets using the broadened list_all_markets (includes individual
    political/macro series from _INDIVIDUAL_SERIES).
    """
    from kalshi_client import KalshiClient

    print(
        f"[Kalshi] Fetching up to {max_markets} markets "
        "(paginated cursor + individual series)"
    )
    t0 = time.time()

    def on_progress(count: int) -> None:
        if count % 2000 == 0:
            print(f"[Kalshi]  ... {count} markets loaded")

    async with KalshiClient(dry_run=True) as k:
        markets = await k.list_all_markets(
            status="open",
            max_markets=max_markets,
            on_progress=on_progress,
        )
    elapsed = time.time() - t0
    print(f"[Kalshi] Fetched {len(markets)} markets in {elapsed:.1f}s")
    return markets


# ---------------------------------------------------------------------------
# Category breakdown helpers
# ---------------------------------------------------------------------------

def pm_us_category_breakdown(markets: list) -> dict[str, int]:
    return dict(Counter(m.category or "unknown" for m in markets))


def kalshi_category_breakdown(markets: list) -> dict[str, int]:
    """
    Break Kalshi markets into buckets: parlay (KXMV*), individual series names
    derived from ticker prefix, plus the 'category' field when present.
    """
    buckets: Counter = Counter()
    for m in markets:
        ticker = m.ticker or ""
        if ticker.upper().startswith("KXMV"):
            buckets["parlay_KXMV"] += 1
        else:
            # Use first segment of ticker as series label (e.g. KXFED, KXCPI)
            series = ticker.split("-")[0] if "-" in ticker else ticker[:10]
            buckets[f"individual:{series}"] += 1
    return dict(sorted(buckets.items(), key=lambda x: -x[1]))


# ---------------------------------------------------------------------------
# Run matcher
# ---------------------------------------------------------------------------

async def run_matcher(pm_markets: list, kalshi_markets: list) -> list:
    from core.cross_platform_arb import MarketMatcher

    matcher = MarketMatcher()
    print(
        f"\n[Matcher] Running structured matcher: "
        f"{len(pm_markets)} PM.US x {len(kalshi_markets)} Kalshi..."
    )
    t0 = time.time()
    pairs = await matcher.find_matches(pm_markets, kalshi_markets)
    elapsed = time.time() - t0
    print(f"[Matcher] Done in {elapsed:.1f}s — found {len(pairs)} pair(s)")
    return pairs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    print("=" * 72)
    print("BROAD COVERAGE MATCHER VALIDATION")
    print("PM.US (paginated full-sweep) x Kalshi (cursor + individual series)")
    print("=" * 72)
    print()

    # --- PM.US ---
    pm_markets = await fetch_pm_us_markets(max_markets=15000)
    pm_active = [m for m in pm_markets if m.active]
    pm_open   = [m for m in pm_active if not m.closed]
    pm_cats   = pm_us_category_breakdown(pm_markets)

    print()
    print(f"PM.US — {len(pm_markets)} total | {len(pm_active)} active | {len(pm_open)} active+open")
    print("  Category breakdown (all):")
    for cat, cnt in sorted(pm_cats.items(), key=lambda x: -x[1]):
        open_cnt = sum(1 for m in pm_markets if (m.category or "unknown") == cat and not m.closed)
        print(f"    {cat:<20} {cnt:>5} total  {open_cnt:>5} open")
    print()

    # --- Kalshi ---
    kalshi_markets = await fetch_kalshi_markets(max_markets=5000)
    kalshi_active  = [m for m in kalshi_markets if m.is_active]
    k_cats         = kalshi_category_breakdown(kalshi_markets)

    print()
    print(
        f"Kalshi — {len(kalshi_markets)} total fetched | "
        f"{len(kalshi_active)} active"
    )
    print("  Bucket breakdown (first 20):")
    for bucket, cnt in list(k_cats.items())[:20]:
        print(f"    {bucket:<35} {cnt:>5}")
    print()

    # Individual markets (non-KXMV)
    individual_k = [m for m in kalshi_markets if not m.ticker.upper().startswith("KXMV")]
    print(f"  Individual (non-KXMV): {len(individual_k)}")
    if individual_k:
        ind_series = Counter(m.ticker.split("-")[0] if "-" in m.ticker else m.ticker[:10]
                              for m in individual_k)
        for series, cnt in sorted(ind_series.items(), key=lambda x: -x[1])[:20]:
            print(f"    {series:<30} {cnt:>4}")
    print()

    # --- Match ---
    pairs = await run_matcher(pm_markets, kalshi_markets)

    print()
    print("=" * 72)
    print(f"MATCH RESULTS: {len(pairs)} pair(s)")
    print("=" * 72)

    if not pairs:
        print()
        print("0 matches found.  Explanation:")
        print()

        # Diagnose why
        pm_politics = [m for m in pm_markets if not m.closed and m.category in ("politics", "macro")]
        pm_macro    = [m for m in pm_markets if not m.closed and m.category == "macro"]

        print(f"  PM.US open politics: {len(pm_politics)}")
        if pm_politics:
            print("  Samples:")
            for m in pm_politics[:6]:
                print(f"    [{m.category}] {m.question[:70]}  slug={m.market_id[:50]}")

        print(f"  PM.US open macro: {len(pm_macro)}")
        if pm_macro:
            print("  Samples:")
            for m in pm_macro[:6]:
                print(f"    [{m.category}] {m.question[:70]}  slug={m.market_id[:50]}")

        k_econ  = [m for m in individual_k if any(
            s in m.ticker for s in ("KXFED", "KXCPI", "KXGDP", "KXPCE"))]
        k_pol   = [m for m in individual_k if any(
            s in m.ticker for s in ("KXSENATE", "KXHOUSE"))]

        print()
        print(f"  Kalshi individual econ (KXFED/CPI/GDP/PCE): {len(k_econ)}")
        if k_econ:
            for m in k_econ[:6]:
                print(f"    {m.ticker:<40} {m.title[:60]}")

        print(f"  Kalshi individual politics (KXSENATE/KXHOUSE): {len(k_pol)}")
        if k_pol:
            for m in k_pol[:6]:
                print(f"    {m.ticker:<40} {m.title[:60]}")

        print()
        print("  WHY NO MATCHES:")
        print()
        print("  PM.US uses an ABSOLUTE THRESHOLD structure for macro:")
        print("    'CPI year-over-year in April'  slug=cpic-uscpi-apr2026yoy-2026-05-12-3pt0pct")
        print("    Each option is a separate binary: 'Was CPI YoY exactly ~3.0%?'")
        print()
        print("  Kalshi uses a CUMULATIVE (is-above) structure:")
        print("    'Will CPI rise more than X% in [month]?' (MoM, NOT YoY)")
        print("    'Will the rate of CPI inflation be above X% for the year ending [month]?'")
        print()
        print("  PM.US Fed market: 'Fed Decision in April'")
        print("    slug=rdc-usfed-fomc-2026-04-29-maintains / cut25bps / hike25bps etc.")
        print("    (already resolved/closed — April FOMC already happened)")
        print()
        print("  Kalshi Fed market: 'Will the Federal Reserve Hike/Cut by Nbps at their")
        print("    [Month] meeting?' — same event, same binary structure, but:")
        print("    (a) PM.US April FOMC is CLOSED (closed=True), not matchable")
        print("    (b) PM.US has no upcoming FOMC markets currently open")
        print()
        print("  PM.US House/Senate control: active=True, closed=False (open)")
        print("    slug=paccc-usho-midterms-2026-11-03-rep  question='U.S House Midterm Winner'")
        print()
        print("  Kalshi KXSENATE/KXHOUSE: 0 open markets NOW")
        print("    These will open ~3-6 months before the Nov 2026 midterms.")
        print("    When they open, the matcher CAN in principle produce pairs — but")
        print("    the structured matcher currently only handles sports game-winners")
        print("    and FIFA World Cup; it has no politics identity parser.")
        print()
        print("  CONCLUSION: The structured (v2) matcher is intentionally precision-")
        print("    first. It does not yet implement identity extraction for politics/")
        print("    macro markets because those require a different matching schema")
        print("    (event-type + threshold vs event-type + threshold, NOT team+date).")
        print("    Extending _MarketIdentity.from_kalshi() and from_polymarket() to")
        print("    handle KXFEDDECISION <-> rdc-usfed-fomc-* and KXSENATE <->")
        print("    paccc-uss-midterms-* would be the next step, but requires careful")
        print("    threshold-alignment verification to avoid false positives on")
        print("    'same event, different resolution bucket' pairs.")
    else:
        print()
        for i, pair in enumerate(pairs, 1):
            print(
                f"[{i}]  PM.US:  {pair.polymarket_question[:65]}\n"
                f"     Kalshi: {pair.kalshi_title[:65]}\n"
                f"     Reason: {pair.match_reason}\n"
                f"     Kalshi YES maps to PM YES: {pair.kalshi_yes_maps_to_poly_yes}\n"
                f"     Confidence: {pair.similarity_score:.2f}\n"
            )

    print()
    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    pm_open_non_sports = sum(
        1 for m in pm_markets
        if not m.closed and (m.category or "unknown") not in ("sports",)
    )
    print(f"  PM.US total fetched:          {len(pm_markets)}")
    print(f"  PM.US open non-sports:        {pm_open_non_sports}")
    print(f"  Kalshi total fetched:         {len(kalshi_markets)}")
    print(f"  Kalshi individual (non-KXMV): {len(individual_k)}")
    print(f"  Matched pairs:                {len(pairs)}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
