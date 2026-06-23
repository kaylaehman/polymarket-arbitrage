# Alpha-Vantage-Gated Financial Strategy — Design + Plan

**Date:** 2026-06-22  **Status:** approved, pre-implementation
**Goal:** Financial analog of the NWS weather edge. Use Alpha Vantage market data (crypto, ETF/index, commodities, forex) to INFORM the maker's longshot-NO bets on Kalshi FINANCIAL markets — skip the bets where the underlying is dangerously close to the threshold (could be crossed before resolution), keep only those comfortably clear. Gated/paper-first; additive; Kalshi-only for now (PM.US is a separate workstream).

## HARD CONSTRAINT — AV free tier = 25 requests/DAY, 1/sec
The whole design must minimize calls:
- Fetch per UNDERLYING, never per market (one BTC price serves ALL KXBTC markets).
- Heavy TTL cache: price TTL ~4h (configurable), volatility TTL ~24h. On-demand only — fetch an underlying only when there's a qualifying near-term Kalshi market for it.
- Respect 1/sec (serialize + small sleep). On rate-limit/Note/Information response → return cached-or-None, NEVER raise, log once.
- Small underlying set (≈5-8): BTC, ETH, SPX/SPY, NDX/QQQ, WTI oil, gold, maybe EUR/USD. Budget: ~price (every 4h = 6/day) + vol (1/day) per underlying — keep total ≤ ~20/day. Make the underlying list + TTLs config so we stay under 25.

## STEP 0 (do FIRST):
- Discover which Kalshi FINANCIAL series exist + are near-term longshots: probe Kalshi (authed client, like backtest/collect.py) for crypto (KXBTC*, KXETH*…), index (S&P/Nasdaq close), commodity (oil/gold), forex series. Get real ticker formats + how the THRESHOLD + resolution date are encoded (e.g. "BTC ≥ $120k on date"). Confirm which side wins if the underlying spikes. Print real titles. Map each series → an AV underlying + AV function. EXCLUDE any series you can't confidently map.
- Verify the AV REST functions return what's needed (key in `.env` `ALPHAVANTAGE_API_KEY`): CURRENCY_EXCHANGE_RATE (crypto/forex spot), GLOBAL_QUOTE (ETF/stock), WTI/BRENT + commodity funcs, and a daily series (DIGITAL_CURRENCY_DAILY / TIME_SERIES_DAILY) for volatility. Mind the 25/day budget while testing — make few calls.

## Components
- `core/market_data.py`:
  - `AVClient(api_key, price_ttl_s, vol_ttl_s, http)`: `async get_price(underlying)->float|None` (routes to the right AV function per underlying; TTL cache; 1/sec serialize; rate-limit/Note → None, never raise); `async daily_vol(underlying)->float|None` (stdev of daily % returns over ~20d from the daily series; 24h cache; fallback to a per-asset-class default if unavailable). 
  - `parse_financial_ticker(ticker)->FinancialMarket|None` (series→underlying, threshold, comparator/side, resolution date) per confirmed Kalshi formats.
  - `crossing_margin(price, vol, threshold, days_to_resolution)`: expected move `em = price*vol*sqrt(max(days,1))`; z = `(threshold - price)/em`. For a NO bet on "underlying ≥ threshold" (longshot=spike up): SAFER as z grows (threshold many expected-moves above price). 
- Gate in `MakerLongshotStrategy` (additive, same shape as the weather gate; only affects financial-series candidates; non-financial untouched): if ticker parses as financial AND closes within horizon: get price+vol; if unavailable → per `financial.require_data` (default true=skip); else KEEP the NO bet only when `|z| >= financial.min_sigma` on the safe side (underlying comfortably away from threshold in the favorable direction), SKIP when within min_sigma (too crossable).
- Config `financial`: enabled (default true), min_sigma (default 2.5), price_ttl_minutes (240), vol_ttl_hours (24), horizon_days (14), require_data (true), underlyings map (series→underlying→AV function), max_calls_per_day guard.

## Risk / safety
Additive; only affects financial-series candidates; non-financial + weather + structural untouched. AV failure/rate-limit degrades gracefully (skip or structural fallback per config, never crash). Key env-only (not git). Gated by `financial.enabled`. Paper-first. Hard cap on daily AV calls to stay under 25.

## Testing (TDD; mock AV http — no live network)
- parse_financial_ticker on confirmed real examples + unparseable→None.
- AVClient: routes underlying→function; caches (2nd call within TTL = 0 HTTP); rate-limit/Note response → None (swallowed, never raises); 1/sec respected.
- crossing_margin / z math; gate KEEPS at |z|≥min_sigma (safe side), SKIPS within min_sigma, SKIPS-or-fallback when data unavailable per config; non-financial candidates untouched.
- ~16-20 tests; full tests/directional green.

## Verify live (after deploy, gated/paper)
For current near-term financial Kalshi longshots: print parsed (underlying/threshold/date), AV price, vol, days, z, and keep/skip decision. Show daily AV call count stays < 25.

## Out of scope (v1; follow-ups)
Informed YES bets; intraday refresh (free tier can't); options-implied vol (use realized); premium AV tier (note it lifts 25/day if this validates). Adding the AV MCP to OpenClaw/AI-directional is a separate optional wire-up.
