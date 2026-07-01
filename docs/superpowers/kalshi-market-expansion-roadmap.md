# Kalshi Market Expansion Roadmap

**Status:** PARKED pending climate σ calibration (see `2026-07-01-climate-low-temp.md`
and the climate calibration memory). Resume once the reliability report validates σ.

**Principle:** the bot has edge only where a **free, timely, predictive data source**
exists that the market underweights. Each build is another provider (`match()` +
`probability()`) on the shipped `core/directional/climate/` framework (or the existing
sports/macro gates). Every family ships **disabled by default**, PAPER only, and is
enabled per-flag after its own calibration looks sane.

## Priority order (edge × liquidity × data availability)

| # | Build | Kalshi series | Data source | Model shape | Notes |
|---|-------|---------------|-------------|-------------|-------|
| A | **low_temp** | `KXLOW*`/`KXLOWT*` (28) | NWS overnight low | Gaussian over strike | Plan written (`2026-07-01-climate-low-temp.md`); trivial mirror of high_temp. |
| F | **Sports expansion** | Sports (2290) | Sportsbook odds (The Odds API), de-vigged | prob = de-vig book line; bet Kalshi vs book | Existing gate (Odds API + Dixon-Coles). Biggest liquid edge; Kalshi lags the book. Expand leagues + market types (spreads/totals/props). |
| B | **Hurricanes** | `KXHUR*` (58) | NHC 5-day cone + landfall-probability tables | landfall prob for city markets; Poisson for basin counts (`KXHURRICANE-<basin>MAJ-N`) | In-season Jun–Nov. Biggest new weather family. |
| G | **Gas daily** | `KXGASD`, `KXAAAGAS*` (Commodities) | AAA/EIA daily gas price | daily observable → Gaussian/threshold (weather-like) | Same "daily number" shape as temperature. |
| H | **TSA throughput** | `TSAW`, `KXMTA` etc. (Transportation, 43) | TSA daily checkpoint numbers | day-of-week + seasonal trend → count/threshold | Highly predictable; near weather-easy. |
| I | **Spotify streams** | `KXSPOTIFYSONGS*` (Entertainment) | **kworb** (already scraped for the artist model) | projected stream total vs threshold | Reuse existing kworb infra + artist-projection code. |
| C | **Tornado + monthly anomaly** | `KXTORNADO`, `KXHMONTHRANGE` | SPC national monthly climatology; NOAA/NCEI normals | Poisson tail (counts); MTD projection (anomaly) | Statistical, lower liquidity. NOTE: the reliability report (C's other half) is DONE (`reliability.py`). |
| D | **Cross-venue arbitrage** | any weather/climate on Kalshi ∩ Polymarket ∩ PM.US | the venues themselves | YES_a + NO_b < $1 underround (riskless) | Different subsystem (matching + underround), not a forecast model. Reuses the cross-platform matcher. |

## Explicitly OUT (no trainable edge)
- **Politics (2054)** — polls noisy/long-horizon; markets efficient/fraught.
- **Science/Tech (349)**, most **Financials (684)/Companies (297)** — event-driven
  (AI releases, FDA, M&A, earnings KPIs); no free forecast source.
- **Crypto (254)** — efficient; thin vol edge at best.
- **Health (98)** — trainable via CDC FluView/COVID counts, but low liquidity; revisit
  only if the count-market pattern (tornado) proves out.

## Execution
Each build = spec-lite (design already covered by the climate framework) → writing-plans
→ subagent-driven-development with ruflo → merge → enable-per-flag → calibrate. Sports (F)
and cross-venue (D) may warrant their own short specs since they don't ride the
ClimateProvider interface directly.
