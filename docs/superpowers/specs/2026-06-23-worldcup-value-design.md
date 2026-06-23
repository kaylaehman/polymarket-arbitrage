# 2026 WC Value-Betting — Paper Experiment Design

**Status:** EXPERIMENTAL / PAPER ONLY  
**Branch:** `feature/worldcup-value`  
**Isolated from:** live structural-arb, directional-trading, any live execution  
**Date:** 2026-06-23

---

## Part A — Model: Hicruben Dixon-Coles (https://github.com/Hicruben/world-cup-2026-prediction-model)

### What it is

Node.js (ESM, no dependencies, Node 18+). Pure stats:  
Elo ratings → expected-goals formula → Dixon-Coles bivariate Poisson → 1X2 + scoreline matrix.  
Full 50,000-trial Monte Carlo runs on their live site (cup26matches.com), not in this repo.

### Data currency (critical)

- `data/elo-calibrated.json`: 63 WC2026 teams, calibrated on 913 internationals (Oct 2023–Jun 2026), generated 2026-06-11.
- `data/wc2026-results.json`: 43 WC2026 matches through 2026-06-22 (Argentina 2-0 Austria). Updated 2026-06-23T02:12:24Z.
- **Ratings are pre-tournament frozen** (by design). They do not re-calibrate mid-tournament on WC results; WC results condition the bracket simulation only, not team strengths.

### What it outputs (confirmed running)

```
Spain vs Germany (Elo 2010 vs 1926):
  1X2:  win=45.5%  draw=28.3%  loss=26.3%
  xG:   1.56 – 1.14
  Top scorelines: 1-1 (13.6%), 2-1 (9.4%), 1-0 (9.0%), 0-0 (8.3%), 2-0 (8.2%)

Brazil vs Argentina (Elo 1955 vs 1976):
  1X2:  win=33.1%  draw=29.0%  loss=37.9%
  Top scorelines: 1-1 (13.9%), 1-2 (8.6%), 0-0 (8.4%), 2-1 (8.0%), 0-1 (7.9%)
```

### Integration method

**Python-native** — `core/worldcup/model_runner.py` re-implements the same Elo+DC math directly in Python (equations are short, fully readable in `elo.mjs`). No subprocess call needed. Loads `~/wc-model/data/elo-calibrated.json`. Confirmed matching Node output to <0.1pp.

For tournament/group advancement odds (which require the Monte Carlo): pull the live open-data JSON from `https://cup26matches.com/data/probabilities.json` (CC BY 4.0, updated after every match). This is the cleanest path — they run 50k trials with the real conditioning; we just consume it.

### Limitations

1. Ratings frozen pre-tournament — don't capture form signals from WC group stage.
2. No home-field advantage data for WC (all venues are USA/CAN/MEX, model treats as neutral).
3. No injury/squad information.
4. Calibrated on general internationals, not specifically WC pressure situations.

---

## Part B — PM.US World Cup Markets

### Total found: 1,628 WC-tagged active markets (full 9,380 market scan)

| Type | Count | Slug Pattern | Model-priceable? |
|---|---|---|---|
| tournament_winner | 45 | `tec-f-wc-*-winner-{t3}` | Yes (Monte Carlo probs from cup26matches.com/data) |
| group_winner | 34 | `tec-f-wc-*-group{X}-winner-{t3}` | Yes (same) |
| stage_of_elimination | 263 | `aqc-fifa-wc-*-stgelim-{t3}-{stage}` | Yes (advancement probs) |
| goal_scorer (per player) | ~400 | `aachc-*-{t3}-gs-{player}` | No |
| group_stage_goals (team) | ~50 | `aachc-*-gsgoal-{t3}-{n}g` | No (requires xG+team goals) |
| total_goals/penalties | ~50 | `aachc-*-tgs-{n}goa` | Partial (xG sum) |
| top_scorer | 53 | `tec-*-topscorer-{player}` | No |
| other/unclassified | ~733 | various | No |

**All 1,628 are `sportsMarketType: futures`** — binary YES/NO per outcome.  
**Zero per-fixture moneyline (1X2) markets found** — PM.US WC is entirely futures-format.

### Market structure

Each market has `marketSides` array with YES (long=true) and NO (long=false) sides. Price is the YES price (e.g. `"0.137"` = 13.7 cents = 13.7% implied probability). The implied NO price is `1 - YES_price`.

Sample: `tec-f-wc-2026-07-19-winner-esp` → Spain wins WC → YES price $0.137 → implied 13.7%. Our model's tournament-winner prob for Spain is available at `cup26matches.com/data/probabilities.json`.

### Mapping: slug → (team, outcome) → model probability

```
tournament_winner/{team} → probabilities.json["champion"][team_slug]
group_winner/{group}/{team} → probabilities.json["group_winner"][team_slug]
stage_of_elimination/{team}/{stage} → probabilities.json["reaches"][stage][team_slug]
```

The `pmus_wc.py` parser (`scripts/pmus_wc.py`) handles slug → `WCMarketKey` with `team_slug` for model lookups.

---

## Part C — Build Plan

### Architecture: `core/worldcup/` module

```
core/worldcup/
  __init__.py           — module docstring + EXPERIMENTAL label
  model_runner.py       — Dixon-Coles probs (DONE, confirmed working)
  tournament_probs.py   — fetch/cache cup26matches.com/data/probabilities.json
  value_detector.py     — compare model_prob vs market_implied, flag value
  kelly.py              — fractional-Kelly sizing on paper bankroll
  ledger.py             — paper positions SQLite (separate from live store)

scripts/
  pmus_wc.py            — slug parser (DONE, confirmed working)
  worldcup_value_run.py — daily orchestrator (equivalent of daily_maker_report.py)
```

### Daily orchestrator flow

```
1. tournament_probs.fetch() → {team: {champion: p, group_winner: p, reaches_qf: p, ...}}
2. pmus_watcher fetch WC markets → filter to model-priceable types (342 markets)
3. For each market:
   a. parse_slug(slug) → WCMarketKey
   b. market_implied = float(marketSides[YES].price)
   c. model_prob = tournament_probs[key.outcome_type][key.team_slug][key.stage]
   d. edge = model_prob - market_implied
   e. if edge >= VALUE_MARGIN and VALUE_MIN <= model_prob <= VALUE_MAX:
        kelly_f = edge / (1 - market_implied)  # full Kelly
        bet_size = KELLY_FRACTION * kelly_f * PAPER_BANKROLL
        ledger.record(...)
4. Post Discord summary → #trading (labeled EXPERIMENTAL PAPER WC)
```

### Recommended parameters

| Param | Value | Reasoning |
|---|---|---|
| `VALUE_MARGIN` | 0.07 (7pp) | Below 5pp is noise given model calibration error ~2.3pp + market spread; 7pp gives ~3x calibration error buffer |
| `VALUE_MIN` | 0.05 | Don't flag near-zero outcomes where model is unreliable |
| `VALUE_MAX` | 0.90 | Avoid near-certain markets (no edge exists) |
| `KELLY_FRACTION` | 0.20 | Aggressive 25% too risky on uncalibrated single-tournament; 20% is safer |
| `PAPER_BANKROLL` | $500 | Enough to see meaningful position sizes; obviously fake |
| `MIN_LIQUIDITY` | YES_price * 100 > $10 notional depth | Skip if orderbook is thin |
| `INCLUDE_TYPES` | tournament_winner, group_winner, stage_of_elimination | Skip goal_scorer/totals (model can't price) |
| `SKIP_TYPES` | goal_scorer, group_stage_goals, total_goals, hat_tricks, top_scorer | No principled model probability |

### Why skip exact-score / 1X2 per-fixture

PM.US has **zero per-fixture moneyline markets** (all WC markets are tournament-futures format). The scoreline matrix from `model_runner.py` is available but no market to match it against. Not a current blocker — store the capability for if PM.US adds futures-format per-game markets.

### Honest risks

1. **Model staleness mid-tournament**: Elo calibrated pre-WC. 43 WC matches have produced significant upsets (Spain 0-0 Cape Verde, Netherlands 2-2 Japan). Market prices embed that information; our model does not. The cup26matches.com probabilities.json does condition on real results for bracket advancement (their Monte Carlo re-runs), which partly mitigates this.

2. **Calibration ≠ edge**: A well-calibrated model and a well-calibrated market can both be "right." Even if model ECE is 2.3%, the market may have absorbed the same information from 100x more signal sources. 7pp margin is a guess at a "safe" threshold, not a proven edge.

3. **No backtest possible for 2026 WC**: Tournament started June 11 — there is no held-out test set. All apparent edges are in-sample. We have 43 matches played, ~61 remaining (plus knockout rounds). This is a tiny sample.

4. **One-shot tournament**: Unlike a betting market with thousands of recurring events, this tournament ends July 19. Any strategy cannot be validated until it's over, and there's no "run it again next week" to bootstrap confidence.

5. **Mapping gaps**: Some team abbreviations in PM.US slugs may not map to Hicruben slugs (the `ABBREV_TO_SLUG` dict is ~90% complete; unknown abbrevs get `team_slug=None` and are silently skipped). Need to audit any `is_model_priceable=False` cases due to missing mapping.

6. **Live data dependency**: `cup26matches.com/data/probabilities.json` is a third-party URL. If it goes down or changes format, tournament/group advancement probs are unavailable (fallback: mark those market types as unresolvable that run).

7. **market spread**: PM.US has a spread (YES price ≠ 1 - NO price). The mid is closer to true implied prob; using the YES ask overstates the cost. Use `(YES_ask + (1 - NO_ask)) / 2` for fair implied mid.

### Scaffold committed (this PR)

- `core/worldcup/__init__.py` — module header
- `core/worldcup/model_runner.py` — Dixon-Coles integration, confirmed returning real probs
- `scripts/pmus_wc.py` — slug parser, confirmed parsing all real PM.US WC market types

### Remaining build tasks (not in this PR)

1. `core/worldcup/tournament_probs.py` — fetch+cache probabilities.json (30min TTL)
2. `core/worldcup/value_detector.py` — edge detection + value flagging
3. `core/worldcup/kelly.py` — fractional Kelly on paper bankroll (reuse `core/kelly.py` logic)
4. `core/worldcup/ledger.py` — SQLite paper ledger separate from directional store
5. `scripts/worldcup_value_run.py` — daily orchestrator
6. Wire Discord report to `ALERT_DISCORD_WEBHOOK` with `[EXPERIMENTAL PAPER WC]` prefix
7. Tests: `tests/worldcup/test_model_runner.py`, `test_pmus_wc.py`, `test_value_detector.py`

---

## Fallback: if cup26matches.com probabilities.json becomes unavailable

Implement Monte Carlo in Python using `model_runner.py` + the `data/wc2026-results.json` bracket state. Would require:
- Parse current bracket from wc2026-results.json (group standings, knockouts)
- Implement best-third-place tiebreaker rules (complex but documented)
- Run 10,000 MC trials (fast enough in Python at this scale)

This is ~200 lines and is a solid fallback, but the live probabilities.json is simpler and more accurate (50k trials + pre-team-strength adjustments).
