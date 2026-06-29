"""Phase 5 operational CLI: backfill | project | backtest | dry-run-edge.

  python -m music_intel.cli backfill                # scrape kworb -> store snapshots
  python -m music_intel.cli project --chart hot100  # print today's projection
  python -m music_intel.cli backtest                # Brier vs Billboard truth (out-of-sample)
  python -m music_intel.cli dry-run-edge            # discover markets, print edges (NO trades)

Commands take injected dependencies so they are unit-testable without network.
NONE of these commands ever place a trade — `dry-run-edge` is alert/print only,
honoring the NO AUTO-EXECUTION policy (see MUSIC_INTEL.md).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


# ── command implementations (deps injected) ──────────────────────────────────

async def cmd_backfill(source, store, chart: str, *, as_of: Optional[date] = None) -> int:
    """Fetch one chart from `source` and persist every row. Returns row count."""
    records = await source.fetch(chart, as_of=as_of)
    for r in records:
        store.record_snapshot(r)
    print(f"backfill {chart}: stored {len(records)} snapshot(s) from {source.name}")
    return len(records)


async def cmd_project(source, chart: str, *, artist: str = "", title: str = "",
                      as_of: Optional[date] = None, cfg=None) -> Optional[dict]:
    """Project #1 probability for a target (defaults to the field leader)."""
    from music_intel.projection import project_number_one
    from music_intel.config import MusicIntelConfig
    cfg = cfg or MusicIntelConfig()
    records = await source.fetch(chart, as_of=as_of)
    if not records:
        print(f"project {chart}: no data from {source.name}")
        return None
    if not artist and not title:
        lead = records[0]
        artist, title = lead.artist, lead.title
    proj = project_number_one(records, artist, title,
                              stream_eu=cfg.stream_eu, margin_k=cfg.margin_k, as_of=as_of)
    out = {
        "chart": proj.chart, "target": proj.target, "prob": proj.prob,
        "band": [proj.prob_low, proj.prob_high], "confidence": proj.confidence,
        "projected_rank": proj.projected_rank, "drivers": proj.drivers,
    }
    print(json.dumps(out, indent=2, default=str))
    return out


async def cmd_dry_run_edge(engine, chart: str, *, as_of: Optional[date] = None) -> list:
    """Run the coordinator and PRINT edges. Never trades (engine is alert-only)."""
    assert engine.execution_enabled() is False, "execution must be disabled"
    res = await engine.run_once(chart, as_of=as_of)
    print(f"dry-run-edge {chart}: {res.snapshot_count} snapshot(s), "
          f"{res.market_count} market(s), {len(res.signals)} signal(s)")
    for s in res.signals:
        print(f"  [{s.source}] {s.side} {s.target!r}  model={s.model_prob:.2f} "
              f"mkt={s.market_prob:.2f} edge={s.net_edge:+.3f} conf={s.confidence:.2f}")
    if not res.signals:
        print("  (no qualifying edges — this is the normal case)")
    return res.signals


def cmd_backtest(weeks, *, bins: int = 10, train_frac: float = 0.6) -> Optional[dict]:
    """Out-of-sample Brier over (week, kworb_records, actual_#1) tuples."""
    from music_intel.calibration import backtest
    if not weeks:
        print("backtest: no historical weeks available yet")
        return None
    res = backtest(weeks, bins=bins, train_frac=train_frac)
    out = {"weeks": len(weeks), "brier_out_of_sample": res.brier_out_of_sample,
           "brier_in_sample": res.brier_in_sample}
    print(json.dumps(out, indent=2))
    return out


# ── wiring ───────────────────────────────────────────────────────────────────

def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="music_intel.cli", description="Music chart intelligence")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("backfill", "project", "backtest", "dry-run-edge"):
        sp = sub.add_parser(name)
        sp.add_argument("--chart", default="spotify_us_daily")
        if name == "project":
            sp.add_argument("--artist", default="")
            sp.add_argument("--title", default="")
    return p


async def _run(args) -> int:
    """Construct real deps (live network) and dispatch. Kept thin + untested."""
    import httpx
    from music_intel.config import MusicIntelConfig
    from music_intel.store import MusicStore
    from music_intel.sources.kworb import KworbSource
    from music_intel.sources.markets import discover_all
    from music_intel.engine import MusicIntelEngine
    from music_intel.alerts import CoreAlertSink

    cfg = MusicIntelConfig()
    async with httpx.AsyncClient(headers={"User-Agent": cfg.user_agent}, timeout=20) as http:
        source = KworbSource(http=http)
        store = MusicStore()
        if args.command == "backfill":
            await cmd_backfill(source, store, args.chart)
        elif args.command == "project":
            await cmd_project(source, args.chart, artist=args.artist, title=args.title, cfg=cfg)
        elif args.command == "dry-run-edge":
            engine = MusicIntelEngine([source], lambda: discover_all(http),
                                      store=store, alert_sink=CoreAlertSink(), cfg=cfg)
            await cmd_dry_run_edge(engine, args.chart)
        elif args.command == "backtest":
            cmd_backtest([])  # historical week assembly is a follow-up
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_argparser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
