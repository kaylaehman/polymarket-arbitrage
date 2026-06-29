# music_intel — Music Chart Prediction Intelligence

Self-contained module that projects Billboard **Hot 100** / **Billboard 200**
outcomes from streaming/airplay/sales velocity and surfaces pricing **edges**
against Polymarket/Kalshi markets that resolve on those charts.

It is **alert-only**. It never trades (see *No-execution policy* below).

> Full design rationale lives in [`MUSIC_INTEL.md`](MUSIC_INTEL.md). This README
> is the operator's quick reference.

## Pipeline (5 independently-tested phases)

```
ingest ──► project ──► match markets ──► edge ──► alert (manual action)
kworb     equiv-unit   Gamma + Kalshi    conf-     Telegram
(scrape)  heuristic    discovery         scaled    (tagged signal)
```

| Phase | File | What it does |
|-------|------|--------------|
| 1 | `sources/` | `ChartDataSource` adapters: `kworb` (scrape, backbone), `billboard` (ground truth, **calibration only**), `luminate` (paid stub, disabled), `markets` (market discovery) |
| 1 | `store.py`, `ratelimit.py` | SQLite persistence + polite rate limiting |
| 2 | `projection.py` | Transparent equivalent-unit heuristic → `{units, prob, band, confidence, drivers}` |
| 3 | `calibration.py` | Replay kworb history vs Billboard truth → Brier + calibration curve (out-of-sample) |
| 4 | `edge.py` | model-prob vs market price, net of fees/slippage, **confidence-scaled threshold** |
| 5 | `engine.py`, `alerts.py`, `config.py` | Coordinator → tagged `ChartSignal` + Telegram alert |

## Trust hierarchy

**Luminate (paid, stub) > Billboard (ground truth) > kworb (scraped, backbone).**

- **kworb.net** drives the live projection (free, scraped).
- **Billboard** (`billboard.py`) is **ground truth for backtest only** — wired into
  `calibration.py`, *never* imported by `projection.py`. No leakage, by construction.
- **Luminate** ships disabled; set `LUMINATE_API_KEY` to light up the (stub) seam.
  When present it becomes the highest-trust source and downranks scraped feeds.

## Key invariants

- **Confidence propagates end-to-end.** Low confidence → *higher* required edge,
  never a confident bet. A tight chart race → wide probability band → blocked edge.
- **"No market this week" is first-class.** Discovery returning nothing is logged at
  INFO (never an error); the projection still runs and is recorded, so a
  backtestable track record accrues even when no market exists.
- **Polite scraping.** Rate-limited, cached, real User-Agent, backoff. Honors ToS;
  degrades gracefully (returns `[]`) on a block — never evades.
- **Calibrated heuristics, not ground truth.** Billboard's exact equivalent-unit
  coefficients and tracking-week cutoffs are *not public*. The constants in
  `config.py` (`stream_eu`, `sale_eu`, `margin_k`, …) are tunable approximations.

## No-execution policy

This module **never** places a trade. `MusicIntelEngine.execution_enabled()`
always returns `False`; the `ENABLE_CHART_EXECUTION` env flag is a documented seam
for a *future external* consumer and is **never read to act on, nor flipped, inside
this module**. Signals integrate into the existing `intelligence/` pipeline as a
**tagged** source (`source="chart-intel"`, reasoning prefixed `[chart-intel]`) so
they never silently outvote NewsAPI/Claude. Humans act on alerts.

## Config (`music_intel:` block in config.yaml)

```yaml
music_intel:
  enabled: false            # master switch for the module
  charts: [hot100, billboard200]
  stream_eu: 1250.0         # paid streams per chart unit (heuristic)
  margin_k: 12.0            # logistic steepness: unit-margin -> P(#1)
  request_min_interval_s: 2.0
  daily_call_cap: 500
  alerts_enabled: true
  edge:
    base_threshold: 0.05    # min net edge at full confidence
    threshold_conf_scale: 0.15  # extra edge demanded as confidence -> 0
    confidence_floor: 0.25
    min_liquidity: 100.0
    max_days_to_resolution: 120.0
    fee: 0.02
    slippage: 0.01
```

## Environment

```bash
LUMINATE_API_KEY=          # blank → Luminate stays a disabled stub
ALERT_TELEGRAM_BOT_TOKEN=  # reused from the main alerts config
ALERT_TELEGRAM_CHAT_ID=
ENABLE_CHART_EXECUTION=false   # never flipped by this module
```

## Tests

Fixture-based, no live network:

```bash
export PYTHONPYCACHEPREFIX=/tmp/music-pyc
.venv-dev/bin/pytest tests/music_intel/ -q -p no:cacheprovider
```
