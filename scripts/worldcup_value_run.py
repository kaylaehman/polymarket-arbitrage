"""
scripts/worldcup_value_run.py — WC2026 paper value-bet orchestrator.

Flow:
  1. Load Elo ratings and apply WC2026 match results (recalibrate)
  2. Run Monte Carlo bracket simulation
  3. Fetch live WC markets from PM.US
  4. Detect value bets (edge > VALUE_MARGIN)
  5. Log paper bets to SQLite ledger

EXPERIMENTAL / PAPER only.  No live execution.

Usage:
    python -m scripts.worldcup_value_run
    python -m scripts.worldcup_value_run --sims 5000 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Project-root on sys.path so we can import core.* and scripts.*
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from core.worldcup.config import (
    N_SIMULATIONS,
    MIN_LIQUIDITY,
    WC_MODEL_PATH_DEFAULT,
    PAPER_BANKROLL,
    VALUE_MARGIN,
    KELLY_FRACTION,
)
from core.worldcup.recalibrate import load_and_recalibrate
from core.worldcup.simulate import simulate_tournament, ADVANCED_32
from core.worldcup.value_detector import detect_value
from core.worldcup.ledger import Ledger
from scripts.pmus_wc import parse_slug, is_model_priceable, MODEL_PRICEABLE_TYPES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [wc_value_run] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wc_value_run")

GATEWAY = "https://gateway.polymarket.us"
PAGE_SIZE = 100
PAGE_DELAY = 0.3


# ---------------------------------------------------------------------------
# Market fetching (adapted from pmus_watcher.py)
# ---------------------------------------------------------------------------

async def _fetch_wc_markets(client: httpx.AsyncClient) -> list[dict]:
    """Fetch active PM.US markets and return those that look like WC markets."""
    markets: list[dict] = []
    offset = 0

    while True:
        try:
            resp = await client.get(
                f"{GATEWAY}/v1/markets",
                params={
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "active": "true",
                    "closed": "false",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning(f"Fetch stopped at offset={offset}: {exc}")
            break

        data = resp.json()
        batch = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        if not batch:
            break

        for m in batch:
            slug = m.get("slug", "")
            key = parse_slug(slug)
            if is_model_priceable(key):
                # Check minimum liquidity
                liq = m.get("liquidity") or m.get("volume") or 0
                try:
                    liq = float(liq)
                except (ValueError, TypeError):
                    liq = 0.0
                if liq >= MIN_LIQUIDITY:
                    markets.append(m)

        log.debug(f"Scanned offset={offset}, WC priceable so far={len(markets)}")
        offset += PAGE_SIZE
        await asyncio.sleep(PAGE_DELAY)

    log.info(f"Found {len(markets)} priceable WC markets from PM.US")
    return markets


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def run(n_simulations: int, dry_run: bool, db_path: Path | None) -> None:
    log.info("=== WC2026 Value Run START ===")

    # Step 1: Recalibrate Elo
    log.info("Recalibrating Elo from WC2026 results...")
    model_path = Path(WC_MODEL_PATH_DEFAULT)
    ratings = load_and_recalibrate(model_path)
    top5_advanced = sorted(
        ((t, round(r, 1)) for t, r in ratings.items() if t in ADVANCED_32),
        key=lambda x: -x[1],
    )[:5]
    log.info(f"Recalibration complete. Top 5 by Elo: {top5_advanced}")

    # Step 2: Simulate bracket
    log.info(f"Running {n_simulations:,} bracket simulations...")
    sim_probs = simulate_tournament(ratings, n_simulations=n_simulations)
    top5 = sorted(sim_probs.items(), key=lambda x: -x[1])[:5]
    log.info(f"Top-5 win probabilities: {[(t, f'{p:.3f}') for t, p in top5]}")

    # Step 3: Fetch markets
    log.info("Fetching WC markets from PM.US...")
    async with httpx.AsyncClient() as client:
        markets = await _fetch_wc_markets(client)

    if not markets:
        log.warning("No priceable WC markets found. Check PM.US connectivity.")
        return

    # Step 4: Detect value
    value_bets = detect_value(
        sim_probs,
        markets,
        value_margin=VALUE_MARGIN,
        kelly_fraction=KELLY_FRACTION,
        paper_bankroll=PAPER_BANKROLL,
    )

    if not value_bets:
        log.info("No value bets found above threshold.")
    else:
        log.info(f"Found {len(value_bets)} value bet(s):")
        for vb in value_bets:
            log.info(
                f"  {vb.team_slug:20s} | model={vb.model_prob:.3f} "
                f"price={vb.market_price:.3f} edge={vb.edge:+.3f} "
                f"stake=${vb.kelly_stake:.2f} | {vb.slug}"
            )

    # Step 5: Record paper bets
    if dry_run:
        log.info("[DRY RUN] Skipping ledger writes.")
    else:
        ledger = Ledger(db_path)
        for vb in value_bets:
            bet_id = ledger.record_bet(
                slug=vb.slug,
                outcome_type=vb.outcome_type,
                team_slug=vb.team_slug or "",
                model_prob=vb.model_prob,
                market_price=vb.market_price,
                edge=vb.edge,
                stake=vb.kelly_stake,
            )
            log.info(f"  Recorded paper bet id={bet_id} for {vb.team_slug}")

        summary = ledger.summary()
        log.info(
            f"Ledger summary: total={summary['total_bets']} open={summary['open_bets']} "
            f"staked=${summary['total_staked'] or 0:.2f} pnl=${summary['total_pnl'] or 0:.2f}"
        )

    log.info("=== WC2026 Value Run DONE ===")


def main() -> None:
    parser = argparse.ArgumentParser(description="WC2026 paper value-bet run")
    parser.add_argument("--sims", type=int, default=N_SIMULATIONS, help="Monte Carlo simulations")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to ledger")
    parser.add_argument("--db", type=str, default=None, help="Override DB path")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else None
    asyncio.run(run(args.sims, args.dry_run, db_path))


if __name__ == "__main__":
    main()
