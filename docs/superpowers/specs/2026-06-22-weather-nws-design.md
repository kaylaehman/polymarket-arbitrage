# Forecast-Gated Weather Strategy (NWS) — Design + Plan

**Date:** 2026-06-22  **Status:** approved, pre-implementation
**Goal:** Use free/keyless NWS forecasts (`api.weather.gov`) to turn the maker's BLIND structural NO bets on weather longshots (KXHIGH* — ~83% of live flow) into INFORMED bets. Primary win: SKIP the weather NO bets where the forecast high is too close to (or above) the threshold — these are the tail-loss cases (−$0.92 in backtest, 7% of trades). Gated/paper-first; additive; never touches the live arb.

## Why
Weather markets like `KXHIGHNY-26JUN23-B78.5` ("NYC high ≥ 78.5°F on Jun 23") are currently bet NO purely on structural bias (extreme threshold ⇒ unlikely ⇒ NO). The backtest's losses were when the temp actually hit. NWS daily-high forecasts (MAE ~2–3°F at 1–3 days) let us compute a margin and skip the risky ones, raising win-rate and making the edge defensible. Cross-checked live: KXHIGHNY-26JUN23-B78.5, NWS forecast = 75°F vs threshold 78.5 ⇒ margin −3.5°F (favored but thin).

## STEP 0 (do FIRST — confirm reality):
- Pull a few live KXHIGH* market TITLES from Kalshi (via the bot's authed client — see backtest/collect.py for the working instantiation) to CONFIRM the exact ticker/threshold semantics: is `B78.5` a `≥ 78.5°F` threshold, a `< 78.5` boundary, or a range bucket? Confirm which side (YES/NO) corresponds to "temp hits the high". Get the parsing exactly right — the whole edge depends on it.
- Confirm the CITY + resolution STATION each series resolves on (KXHIGHNY, KXHIGHCHI, KXHIGHLAX, KXHIGHMIA, KXHIGHAUS, + any others discovered). Kalshi temp markets resolve on a SPECIFIC station (e.g. NYC=Central Park). Map each series → that station's lat/lon (NWS forecast gridpoint) for accuracy. Document the mapping + any series you can't confidently map (skip those rather than guess).

## Components
- `core/weather.py`:
  - `parse_weather_ticker(ticker) -> WeatherMarket(series, date, threshold, side_that_wins_if_hot)` — robust to the confirmed format; returns None if unparseable.
  - `SERIES_STATION: dict[series -> (lat, lon, label)]` — only the confirmed ones.
  - `async forecast_high(series, date, *, http) -> float | None` — NWS: `/points/{lat,lon}` → forecast URL → daily `isDaytime` period matching `date` → `temperature` (°F). REQUIRED `User-Agent` header. TTL-cache per (gridpoint, fetch) — one forecast call covers ~7 days. Returns None if date beyond forecast horizon or fetch fails (degrade gracefully — never raise into the scan loop).
  - `forecast_margin(forecast_high, threshold) -> float` (= forecast − threshold).
- Gate in the maker (cleanest as an additional filter on weather candidates; keep non-weather behavior identical): for a candidate whose ticker parses as a weather market AND the market resolves within `forecast_horizon_days`: fetch forecast; if forecast unavailable → fall back to current structural behavior (or skip — make it a config choice `weather.require_forecast`); if available, require `margin <= -safe_margin_f` for a NO bet (forecast comfortably below the "hot" threshold). If `-safe_margin_f < margin` (forecast near/above threshold) → SKIP (too risky). 
- Config `weather`: `enabled` (default true), `safe_margin_f` (default 4.0), `forecast_horizon_days` (default 7), `require_forecast` (default true → skip weather markets we can't forecast rather than bet blind), `stations` (the series→station map, overridable in config.yaml).

## Risk / safety
- Additive; weather gate only AFFECTS weather-series candidates. Non-weather (CABLEAVE, macro) unchanged. NWS failure degrades gracefully (skip or structural fallback, never crash). Keyless, no secrets. Gated by `weather.enabled`. Paper-first.

## Testing (TDD; mock NWS http — no live network in tests)
- `parse_weather_ticker` on real confirmed examples (+ unparseable → None).
- `forecast_high` parses the NWS period for a target date; returns None beyond horizon / on HTTP error (swallowed).
- Gate: NO-bet KEPT when margin ≤ −safe_margin; SKIPPED when forecast near/above threshold; SKIPPED (or structural-fallback per config) when forecast unavailable; non-weather candidates pass through untouched.
- ~12–16 tests; full tests/directional stays green.

## Verify live (after deploy, gated/paper)
- For the current live weather positions, print: parsed (series/date/threshold), NWS forecast high, margin, and the gate decision — confirm it matches intuition (e.g. KXHIGHNY-26JUN23-B78.5: 75 vs 78.5 ⇒ −3.5 ⇒ within 4° safe_margin so SKIP/marginal). Show how many weather candidates the gate now keeps vs skips per cycle.

## Out of scope (v1; note as follow-ups)
- Informed YES bets (bet the longshot when forecast favors a hit + market underprices it).
- Dynamic cancel/re-price of resting NO orders as the forecast updates toward the line.
- Historical forecast backtest (archived NWS forecasts aren't readily available) — validate FORWARD instead via the daily report (weather resolves daily → fast). A NOAA CDO token (historical actual temps) could later support a margin-distribution analysis.
