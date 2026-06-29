# Music Chart Prediction Intelligence Module — Spec

**Status:** approved (build via ruflo subagents), 2026-06-29. Self-contained, parallel to `intelligence/`.

## Purpose
Project music-chart outcomes (Billboard Hot 100 / Billboard 200) from streaming/airplay/sales
velocity and surface pricing edges against Polymarket/Kalshi markets that resolve on those charts.
Execution is **manual only** (see policy below).

## Architecture (5 phases, each independently testable)
```
music_intel/
  sources/  base.py (ChartDataSource iface + ChartRecord), kworb.py, billboard.py,
            luminate.py (stub), markets.py (Polymarket Gamma + Kalshi discovery)
  ratelimit.py   polite limiter + conditional-request cache
  store.py       SQLite (snapshots, projections, calibration, market_matches) — mirrors core/directional/store.py
  projection.py  P2 transparent equiv-unit heuristic -> {point_estimate, prob_band, confidence, drivers[]}
  calibration.py P3 replay kworb-history vs Billboard-truth -> Brier + calibration curves (persisted)
  edge.py        P4 question->model_prob, price->implied, edge net fees/slippage, confidence-scaled threshold
  engine.py      P5 coordinator: ingest -> project -> match markets -> edge -> emit tagged signal + alert
  alerts.py      pluggable sink over core.alerts (Telegram)
  cli.py         backfill | project | backtest | dry-run-edge
```

## Data-source trust hierarchy
**Luminate (paid, stub) > Billboard (ground truth) > kworb (scraped, backbone).**
- **kworb.net** — primary free source (daily/weekly streaming US+Global, Apple, YouTube, Shazam, airplay).
  HTML scrape behind `ChartDataSource`. The live projection runs on kworb.
- **Billboard** (`billboard.py` via guoguo12) — actual published Hot 100 / Billboard 200 results.
  **GROUND TRUTH / BACKTEST ONLY.** Wired *only* into `calibration.py`, **never** imported by
  `projection.py` — no leakage, enforced by construction.
- **Luminate** — env-gated stub (`LUMINATE_API_KEY`); disabled unless key set. When present it becomes
  the high-confidence source and downranks scraped sources. Ships as a clean, disabled seam.

## Projection (Phase 2) — transparent, no black box
Equivalent-unit estimate = weighted(streaming on-demand) + airplay impressions + pure sales, with the
Billboard **tracking-week cutoff approximated** and exposed as config constants. Every projection is
explainable from its inputs (`drivers[]`). Output carries a **confidence_score** and a probability
**band**, not a point bet.

**Coefficient/cutoff assumptions (CALIBRATED HEURISTICS, not ground truth — Billboard's exact
equivalent-unit coefficients and tracking-week cutoffs are NOT public):** all live in
`music_intel` config (a `music_intel:` block in `utils/config_loader.py`), e.g.
`stream_eu_per_1000`, `paid_stream_weight`, `sale_eu`, `airplay_weight`, `tracking_week_cutoff_dow`.
Tunable; documented inline.

## Calibration (Phase 3)
Replay historical kworb snapshots against actual Billboard results -> **Brier score + calibration
curves**, persisted. The live model applies the calibration. Report **out-of-sample only** (no
overfitting to a handful of weeks).

## Confidence propagation (end-to-end, non-negotiable)
Low confidence -> **higher** edge threshold, never a confident bet. `edge.py` threshold scales
inversely with `confidence_score`; gates also require confidence > floor, liquidity > min, sane
time-to-resolution.

## "No market this week" is first-class
Market discovery treats no-match as the **normal case**: log cleanly at INFO, never raise, and still
run + persist the projection (so we accumulate a backtestable track record while markets are absent).

## Politeness / ToS
Rate-limited, cached, conditional requests, real User-Agent, exponential backoff on errors. Honor
robots/ToS; if a source blocks programmatic access, log + degrade gracefully — never evade.

## NO AUTO-EXECUTION POLICY (explicit)
This module never trades. The seam to any trade path is present but **off**, guarded by
`ENABLE_CHART_EXECUTION` (default `false`), which is **never flipped inside this module's code**.
Phase 5 emits signals to the existing intelligence pipeline as a **new tagged/weighted source**
(won't silently outvote NewsAPI/Claude) + a Telegram alert. Humans act on alerts.

## Conventions (match the repo)
Python 3.13, `httpx` async, dataclasses (not Pydantic), `sqlite3` store mirroring
`core/directional/store.py`, pytest asyncio-auto with **fixture-based tests (no live network in CI)**,
config in `utils/config_loader.py`, alerts via `core/alerts.py`. New deps: `billboard.py`,
`beautifulsoup4`, `lxml`.
