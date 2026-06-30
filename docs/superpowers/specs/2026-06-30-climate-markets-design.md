# Climate Markets — Design

- **Date:** 2026-06-30
- **Status:** Approved (design); pending implementation plan
- **Scope owner:** directional paper engine
- **Safety:** PAPER ONLY — every candidate is placed with `mode="paper"`; never live.

## 1. Goal

Today the bot only trades Kalshi **daily-high-temperature** markets (`KXHIGH*`) plus
PM.US `tc-temp-*`, using a conservative "longshot-NO safe-margin gate" in
`maker_longshot`. Kalshi lists many more climate/geophysical families. We want to
wire the bot for the rest of the weather/climate families with **both** betting
styles (longshot-NO and directional), behind one shared, calibratable abstraction.

### In scope (families)

**Tier 1 — NWS-backed (reuses the existing station map):**
- Lowest temperature in a city (`KXLOW*`-style)
- Temperature at a specific time (hourly)
- Rain / precipitation ("where/whether it will rain")

**Tier 2 — statistical (new free sources):**
- Monthly temperature anomaly ("June temperature increase")
- Tornado counts ("tornadoes this month")

The existing **daily-high-temperature** family is folded into the same framework so
it gains directional capability too.

### Out of scope

- **Earthquake-magnitude-by-state.** Short-term quakes are ~unpredictable; the only
  edge is market price vs a long-run USGS base rate. Deliberately excluded as
  speculative / high false-edge risk.
- **Macro markets** (CPI, etc.). They already have a directional path via
  `consensus_divergence`; folding them in is a separable follow-on, not this spec.

## 2. Approach (chosen: A — provider registry → probability→edge layer)

The crux is a **probabilistic upgrade**: instead of a hard keep/skip safe-margin,
every family emits a calibrated `P(market resolves YES)`, and one shared edge layer
turns that single number into either bet style. The current safe-margin gate becomes
just "a provider that outputs `p_yes ≈ 0`."

Rejected alternatives:
- **B — extend `maker_longshot` + `consensus_divergence` per family:** bloats two
  already-large files, tangles 5 families together, duplicates calibration.
- **C — one strategy per family:** heavy duplication (each re-implements discovery,
  sizing, dual longshot/directional logic); 5+ new strategies to wire.

## 3. Architecture

```
core/directional/climate/
  base.py        ClimateProvider (ABC), ParsedClimate, ClimateSignal
  registry.py    ordered providers; match(market_id) -> (provider, parsed) | None
  edge.py        (parsed, market_price, signal) -> 0..2 DirectionalCandidates
  providers/
    high_temp.py        # existing daily-high, wrapped as a provider (gains directional)
    low_temp.py         # Tier 1
    hourly_temp.py      # Tier 1
    precip.py           # Tier 1
    monthly_anomaly.py  # Tier 2
    tornado.py          # Tier 2
core/directional/strategies/climate_paper.py   # the strategy that drives the registry
```

### Interfaces

`ClimateProvider` — each family implements two methods:
- `match(market_id: str) -> ParsedClimate | None` — recognise the series and parse
  its params (geo/station, date or hour, threshold/bucket/count, comparator). Returns
  `None` if this provider does not handle the market.
- `async probability(parsed, http, ctx) -> ClimateSignal | None` — returns the
  calibrated probability the market resolves YES; `None` means "no signal, skip"
  (e.g. beyond forecast horizon, data source unavailable).

`ParsedClimate` (dataclass): `family`, `market_id`, `series`, geo/station ref,
`target` (date/hour), `kind` ("threshold" | "bucket" | "count"), `comparator`
("gte"/"lte"/"between"), numeric `lo`/`hi`/`n`.

`ClimateSignal` (dataclass): `p_yes: float ∈ [0,1]`, `confidence: float ∈ [0,1]`,
`source: str`, `drivers: list[tuple[str, float]]`.

### Edge layer (`edge.py`)

Given `parsed`, the live market YES price, and a `ClimateSignal`, emit 0..2
`DirectionalCandidate`s through the **existing** decider / Kelly / executor:

- **Longshot-NO:** `p_yes <= longshot_floor` (default 0.05) → side=NO, small fixed
  size (the existing maker_longshot tail-bet bucket).
- **Directional:** `|p_yes - market_price| >= min_edge` (default 0.10) → bet the
  diverging side (YES if `p_yes > price + min_edge`, NO if `p_yes < price - min_edge`),
  Kelly-sized using `confidence`.
- **Dedup:** if both fire on the same side, emit a single candidate.

## 4. Per-family signal models (how `p_yes` is computed)

### Tier 1 — NWS (the only new modelling is point-forecast → probability)

| Family | Data | `p_yes` model |
|---|---|---|
| Lowest-temp | NWS forecast **min** temp for station/date | `P = ∫ Normal(forecast_low, σ)` over the market's threshold/bucket. σ ≈ 3–4°F at 1-day, widening with lead time |
| Temp-at-time | NWS **hourly** forecast for the target hour | same Gaussian-over-bucket |
| Rain | NWS **PoP** (prob-of-precip); **QPF** for amounts | PoP *is* `P(rain)` directly; ">X inches" → integrate QPF. "Where will it rain" = per-location P, normalised across the outcome set |

The Gaussian-around-the-forecast also replaces the hard safe-margin for the existing
**daily-high** family, giving it directional capability.

### Tier 2 — statistical (new free sources)

| Family | Data | `p_yes` model |
|---|---|---|
| June temp increase (monthly anomaly) | NOAA climate **normals** + month-to-date observed mean | project full-month mean = MTD observed + climatology for remaining days → `P(month_mean ≥ threshold)`. Same shape as the artist YTD projector |
| Tornadoes this month (counts) | SPC storm reports (MTD count) + monthly tornado **climatology** | remaining count ~ `Poisson(λ = rate × days_left)` → `P(total > N)` via the Poisson tail |

### Data sources (all free, TTL-cached per gridpoint/region like NWS)

- **NWS API** (already wired): daily high/low, hourly temp, PoP, QPF.
- **NOAA / NCEI climate normals**: monthly-anomaly baselines.
- **SPC (Storm Prediction Center)**: tornado storm reports + climatology.

## 5. Data flow

```
scanner (liquid markets) → climate_paper strategy
   → registry.match(market_id)          [unmatched markets ignored]
   → provider.probability(parsed)        → ClimateSignal(p_yes, confidence)
   → edge.py                             → 0..2 DirectionalCandidates
   → decider / Kelly → executor (mode="paper") → store
   → tracker.sweep()                     settles via existing kalshi:/pmus: paths
```

No new settlement, sizing, or dashboard work — it reuses the existing pipeline, and
settled positions feed the calibration loop (Section 8).

## 6. Configuration & paper-safety

- New `ClimateCfg` dataclass with **`mode: str = "paper"` baked in from the start**
  (this is the exact missing-field bug that recently stranded settlements — not
  repeating it).
- A `climate:` config block:
  - per-family `enabled` flags — every family ships **disabled by default**; flip on
    one at a time after its calibration report looks sane.
  - `longshot_floor` (default 0.05), `min_edge` (default 0.10).
  - per-family forecast `σ` (temp) / `λ` source (tornado) parameters.
  - exposure caps consistent with the existing per-bucket caps.

## 7. Error handling

- Every `provider.probability()` is wrapped — any data-source failure returns `None`
  → that market is skipped, **never crashing the cycle** (consistent with the
  run_once / sweep resilience already in place).
- NWS / SPC / NOAA calls are TTL-cached per gridpoint/region to respect rate limits.

## 8. Calibration ("calibrate if needed" — the concrete mechanism)

- On every settlement, log `(family, predicted p_yes, actual outcome)`.
- A new **reliability report** extends the existing `validation` / promotion gate: it
  bins predictions per family (e.g. "predicted 5–10% → actually happened ~7%?"). A
  mis-calibrated family (σ too tight, λ off) shows up here and the parameter is
  widened.
- Families start **conservative (wide σ)** and tighten as resolved samples accrue.
  The promotion gate already blocks "ready/live" for a family until it has enough
  resolved trades.

## 9. Testing (TDD per provider)

- `match()` parsing tests: ticker → `ParsedClimate` (covers each series format).
- `probability()` math tests: Gaussian / Poisson / PoP tails with **mocked** data
  sources (no network in unit tests).
- `edge.py` tests: `(p_yes, price)` → correct candidate(s) and side.
- Each provider is independently testable in isolation.

## 10. Build order (phases)

1. **Discovery (must be first):** hit the Kalshi API to confirm the **actual** series
   tickers and bucket encodings for low-temp, hourly, precip, monthly, and tornado
   markets. Without this, `match()` silently matches nothing (the bug class we just
   fixed). Record the confirmed formats in this doc before writing parsers.
2. **Framework:** `base.py`, `registry.py`, `edge.py` + the `climate_paper` strategy
   wired into the engine (disabled). Tests for the edge layer.
3. **high_temp provider:** wrap the existing daily-high logic to emit `p_yes`
   (validates the framework against known-good behaviour; adds directional to temp).
4. **Tier 1 providers:** low_temp, hourly_temp, precip — one at a time, each TDD'd and
   enabled behind its flag.
5. **Tier 2 providers:** monthly_anomaly, tornado — new sources (NOAA normals, SPC),
   each TDD'd and enabled behind its flag.
6. **Calibration report** in `validation`; enable families as their reliability looks
   sane.

## 11. Open questions / risks

- **Real ticker formats unknown** until Discovery (Phase 1) — biggest unknown; gated
  first.
- **Forecast-error σ is a guess** until calibration data accrues — mitigated by
  starting wide + the reliability report.
- **Tornado/monthly climatology granularity** (per-state vs per-region rates) depends
  on what SPC/NOAA expose at a useful resolution — confirm during Phase 5.
- **"Where will it rain" multi-outcome** markets need per-location normalisation;
  if Kalshi encodes these differently than expected, that sub-family may slip to a
  follow-on.
