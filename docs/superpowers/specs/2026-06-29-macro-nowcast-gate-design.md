# Macro Nowcast σ-Gate — Design

**Date:** 2026-06-29
**Status:** Approved (brainstorm) → pending implementation plan
**Sub-project 1 of 3** in the "bet more categories with real knowledge" effort.
Sub-projects 2 (FOMC probability gate) and 3 (Sports consensus gate) are queued
behind this and get their own spec/plan cycles.

## Problem

The maker (`maker_longshot`) bets NO on longshots across categories, but only bets
*well* where it has a domain-knowledge gate that supplies the true odds:

- **Weather** → NWS forecast (`core/weather.py`) — validated edge (93% backtest).
- **Financial** (BTC/ETH/WTI/EUR) → Alpha Vantage price + volatility z-score
  (`core/market_data.py`).
- **Macro** (CPI / PCE / GDP, e.g. `KXCPIYOY-26JUN-T3.9`) → **nothing**. Macro
  candidates pass through on the structural longshot-NO prior alone — i.e. the bot
  is betting them **semi-blind** (it has no idea what the actual CPI print will be).
  The 6+ open CPI paper positions as of 2026-06-29 are exactly these blind bets,
  and the macro category's validation verdict is therefore meaningless.

## Goal

Give the maker a real macro-knowledge gate so CPI/PCE/GDP bets are informed by the
same kind of forecast the weather gate uses — using free, authoritative Federal
Reserve nowcasts. This both **fixes the current blind macro bets** and lets the
macro category accumulate a meaningful validation verdict.

Non-goals: FOMC rate decisions (sub-project 2 — different, directional logic),
sports (sub-project 3), media (no clean free data source — explicitly skipped),
and any change to the weather/financial gates.

## Approach (chosen)

**σ-tail "keep NO" gate**, mirroring `core/weather.py::bucket_gate_keep` and the
Alpha Vantage `crossing_margin` gate. Pull the Fed nowcast for the indicator
(the macro analogue of the NWS forecast), measure how far each Kalshi bucket
threshold sits from the nowcast in standard deviations, and **keep the NO longshot
only when the bucket is a deep tail** (≥ `min_sigma`). If the nowcast can't be
fetched, **skip the candidate** (`require_data: true`) — never bet blind.

Rejected alternatives:
- *Directional Kalshi-vs-model-probability* (bet either side): departs from the
  validated longshot-NO maker and needs a calibrated probability model that is far
  harder to trust/validate. Not now.
- *Fixed absolute margin* (like weather's °F): the right margin differs per
  indicator and ignores each indicator's release uncertainty. The σ approach
  normalizes CPI vs GDP automatically.

## Components

### 1. Data layer — `core/macro_data.py` (new)

Mirrors `core/market_data.py` (financial gate). Pure-ish, async, `httpx`-based,
graceful degradation (never raises into the maker loop).

- `class MacroNowcastClient` with cached fetchers (TTL ~6h; nowcasts update ~daily):
  - `cpi_nowcast()` / `pce_nowcast()` → Cleveland Fed Inflation Nowcasting
    (downloadable data file from clevelandfed.org). Returns the nowcast for the
    relevant horizon (MoM and YoY where applicable).
  - `gdp_nowcast()` → FRED JSON API, series `GDPNOW`
    (`https://api.stlouisfed.org/fred/series/observations?series_id=GDPNOW`),
    needs a free `FRED_API_KEY`.
- `parse_macro_ticker(ticker) -> Optional[MacroMarket]` for `KXCPI*`,
  `KXCPIYOY*`, `KXCPICORE*`, `KXPCECORE*`, `KXGDP*`: extracts indicator + the
  T-type threshold or B-type bucket [lo, hi] and the variant (MoM/YoY/core).
- All network failures / missing data → `None`.

**Implementation risk to pin during planning:** the exact Cleveland Fed nowcast
data endpoint/format (HTML page vs downloadable CSV/JSON) must be confirmed first;
fall back to a FRED series if one carries the CPI/PCE nowcast. GDPNow via FRED is
already confirmed clean JSON.

### 2. Surprise σ

Converts "nowcast vs threshold" into a σ-distance. **Default: a per-indicator σ
seeded from each nowcast's published accuracy** (e.g. CPI MoM ≈ 0.1pp, CPI YoY ≈
0.1–0.15pp, GDP ≈ 0.3–0.5pp annualized near release), held in a small config map
and overridable. Optionally scaled by days-to-release (the nowcast tightens as the
print nears — analogous to the weather forecast horizon). This avoids needing
hard-to-assemble historical nowcast-vs-actual pairs. Refinable later from realized
errors.

### 3. Gate logic — `macro_gate_keep(...)` in `core/macro_data.py`

Called from `core/directional/strategies/maker_longshot.py` for macro-category
candidates, at the same hook point as the weather/financial gates (non-macro
candidates pass through unchanged):

- T-type (`high ≥ T` style): keep NO when `nowcast ≤ T − min_sigma·σ`
  (and the inverse for `≤ T` markets) — i.e. the threshold is implausibly far in
  the tail vs the nowcast.
- B-type bucket [lo, hi]: keep NO when the nowcast is `min_sigma·σ` outside the
  bucket on either side (bucket is a tail outcome) — mirrors
  `weather.bucket_gate_keep`.
- Returns `(keep: bool, reason: str)`; `keep=False` when data unavailable and
  `require_data: true`.

### 4. Config — new `directional.macro:` block + `.env`

```yaml
macro:
  enabled: false            # default OFF → zero behavior change until validated
  min_sigma: 2.0            # keep NO only if threshold ≥ this many σ from nowcast
  require_data: true        # skip (don't bet blind) when nowcast unavailable
  horizon_days: 45          # ignore releases beyond this (nowcast not meaningful)
  sigma:                    # per-indicator surprise σ (overrides)
    CPI: 0.10
    CPIYOY: 0.12
    CPICORE: 0.10
    PCECORE: 0.10
    GDP: 0.40
```
`.env`: `FRED_API_KEY=...` (free from stlouisfed.org).

### 5. Wiring

`DirectionalEngine` builds a `MacroNowcastClient` (process-lived, like the AV
client) and passes it + `macro_cfg` into `MakerLongshotStrategy`, exactly as the
weather/financial gates are wired today. The `category_for_market_id`-style macro
detection already classifies these tickers.

## Data flow

```
scanner → maker_longshot candidate (KXCPIYOY-26JUN-T3.9, category=macro)
  → parse_macro_ticker → (indicator=CPIYOY, threshold=3.9)
  → MacroNowcastClient.cpi_nowcast() (cached)  [skip if None & require_data]
  → macro_gate_keep(nowcast, threshold, σ=CPIYOY, min_sigma)
  → keep? → decider → executor (paper)   |   drop? → record signal placed=0
```

## Error handling

- Every fetch wrapped; any failure → `None` → gate skips the candidate (with a
  logged reason). The maker loop and all other gates are unaffected.
- Missing `FRED_API_KEY` → GDP nowcast unavailable → GDP candidates skipped (CPI/PCE
  unaffected).
- Bad/unparseable ticker → `parse_macro_ticker` returns `None` → candidate
  passes through ungated (same as today) — but logged, so we notice coverage gaps.

## Testing

- Unit: `parse_macro_ticker` across T-type/B-type/variants; `macro_gate_keep`
  keep/skip math for tail vs in-distribution thresholds; graceful-degradation
  (None nowcast → skip when `require_data`, pass when not).
- Mocked-fetch tests for `MacroNowcastClient` (no live network in CI), mirroring
  the AV client tests.
- Live smoke (manual): fetch real CPI/PCE/GDP nowcasts, classify a few current
  Kalshi macro markets, confirm sane keep/skip.

## Validation / rollout

- Ship `macro.enabled: false`; flip on in **paper** only.
- The existing per-category verdict gate tracks macro toward `positive` — watch it
  accumulate net-of-fees with the gate active. Compare blind-macro results (current)
  vs gated-macro results.
- No live-money path touched (maker is paper).

## Out of scope (queued)

- **Sub-project 2 — FOMC probability gate**: Atlanta Fed Market Probability
  Tracker; *directional* (Kalshi price vs implied prob), also feeds the
  multi-outcome arb.
- **Sub-project 3 — Sports consensus gate**: The Odds API free tier (NBA/MLB h2h,
  25 req/day); *directional* vs bookmaker consensus; longshot-NO unvalidated on
  sports.
- **Media**: no clean free data source (Nielsen paywalled, RT/Spotify APIs
  dead) — not pursued.
