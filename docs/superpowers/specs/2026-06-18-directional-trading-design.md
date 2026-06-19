# Directional Trading Mode — Design

**Date:** 2026-06-18
**Status:** Approved design, pre-implementation
**Goal:** Let the bot trade individual Kalshi markets *directionally* — with or without a cross-platform match — behind a `directional.enabled` flag, paper-first, without disrupting the live Kalshi-native arb (real money).

## Decisions (from brainstorming)
- **Venue:** Kalshi-only first (PM.US later).
- **Go-live:** Paper-first, then per-strategy live by explicit flag flip.
- **AI engine:** Reuse our intelligence layer (Claude via OpenClaw proxy = free, real NewsAPI, resolution-criteria-aware). No OpenRouter.
- **Risk envelope (when live):** $30 total directional exposure, $8 max/position, max 4 open — separate from arb caps.
- **Exits:** Hybrid — Safe Compounder holds to resolution; AI-directional gets stop-loss / take-profit / time-based exits.

## Source material (both MIT, deep-dived)
- `ryanfrigo/kalshi-ai-trading-bot`: market ingest (events+nested), **Safe Compounder** pure-math NO-side strategy, EdgeFilter, StopLossCalculator + tracker.
- `Jon-Becker/prediction-market-analysis`: empirical structural-bias findings (longshot/NO bias by price, maker>taker, category edge) + 580-line Kalshi category taxonomy (port verbatim).

## Architecture (Approach A — standalone parallel subsystem)
New self-contained package launched as one independent asyncio task; reuses shared services, never touches the arb loop.

```
core/directional/
  engine.py          # DirectionalEngine: loop, owns strategies
  scanner.py         # KalshiMarketScanner: events+nested -> individual tradeable markets
  strategies/base.py        # Strategy ABC: scan(markets) -> list[DirectionalCandidate]
  strategies/safe_compounder.py   # pure-math NO-side (holds to resolution)
  strategies/ai_directional.py    # IntelligenceEngine + EdgeFilter + structural bias (stop-loss/TP)
  decider.py         # candidate -> Kelly size -> risk-gated DirectionalOrder
  executor.py        # place/cancel via kalshi_client; paper vs live
  tracker.py         # exit loop (stop-loss/TP/time for AI) + resolution settle (all)
  models.py          # DirectionalCandidate, DirectionalOrder, DirectionalPosition
utils/kalshi_categories.py   # ported verbatim (MIT)
utils/structural_bias.py     # repo #1 findings as parameters
```

**Reused unchanged:** `kalshi_client/api.py`, `intelligence/` (IntelligenceEngine/AIAnalyzer/NewsFetcher), `core/kelly.py`.
**Lightly extended:** `core/risk_manager.py` (Order Protocol + directional caps), `config.yaml` (`directional:` block), `run_with_dashboard.py` (+~8 lines to launch the task when enabled), `dashboard/server.py` (read-only panel), `utils/signal_db.py` (directional tables).

## Components
- **Scanner:** paginate `/events?with_nested_markets=true&status=open`, flatten to binary markets, reject parlay/collection tickers (both YES-ask and NO-ask ~= $1), volume floor, category-tag. Output candidate markets.
- **SafeCompounder (no LLM):** estimate true NO prob from YES last price + time-to-expiry; if cheapest NO ask edge >= threshold (default 3 cents), emit NO-buy resting limit 1 cent below ask. Skip sports/entertainment. Hold to resolution.
- **AIDirectional:** per market -> IntelligenceEngine.evaluate() (news+Claude) -> SignalSummary; EdgeFilter (conf >=0.8->3%, >=0.6->5%, else 8%); fuse structural-bias score; emit BUY YES/NO + reasoning.
- **Decider:** candidate -> Kelly size (core/kelly.py) capped by directional caps -> DirectionalOrder; risk-gate via risk_manager.check_order().
- **Executor:** paper = record intended + simulated fill at observed price; live = kalshi_client.place_order (Safe Compounder resting limit, AI marketable limit) + pre-flight balance guard.
- **Tracker:** AI positions get stop-loss/TP/time exits (port StopLossCalculator); resolution sweep settles all + books P&L; Safe Compounder = resolution sweep only.
- **Persistence:** extend utils/signal_db.py with directional_positions + directional_signals (SQLite), survives restart.

## Data flow
```
loop (every scan_interval, only if directional.enabled):
  markets = scanner.scan()
  for strategy in enabled_strategies:
     for c in strategy.scan(markets):
        order = decider.decide(c)          # Kelly + risk_manager.check_order
        if order: executor.place(order)    # paper or live per-strategy
  tracker.sweep()                          # exits (AI) + resolution settle (all)
```

## Risk, safety & gating
- RiskConfig: directional_max_total_exposure=30, directional_max_position=8, directional_max_open=4 (tracked separately; cannot starve arb). Global kill switch / daily-loss / drawdown still apply.
- Order Protocol in risk_manager so Kalshi directional orders use the same check_order().
- Per-strategy mode paper|live. Paper simulates fills at observed prices + books would-be P&L on resolution. Live = explicit per-strategy flag; human gate = positive paper expectancy over N resolved trades.
- Isolation: task created only if enabled; loop wrapped in try/except (failure logs, never touches arb); AI path fail-safe (error -> skip candidate, never trade on bad signal).

## Error handling
Scanner/API: retry+skip (rate-limit aware). AI/news: skip candidate. Executor: log+continue. Tracker: log+retry next sweep. Directional can never crash the live arb.

## Config (new block)
```yaml
directional:
  enabled: false
  scan_interval_seconds: 120
  markets_per_cycle: 25
  category_exclude: []
  caps: { total_exposure: 30, max_position: 8, max_open: 4 }
  safe_compounder: { mode: paper, min_edge_cents: 3, skip_categories: [sports, entertainment] }
  ai_directional:  { mode: paper, min_confidence: 0.60, min_edge_pct: 0.05, kelly_fraction: 0.25,
                     stop_loss_pct: 0.30, take_profit_pct: 0.50, max_hold_hours: 72 }
```

## Testing (TDD)
Unit (mocked kalshi_client + intelligence, no live API): parlay filter; SafeCompounder NO-prob + edge math; EdgeFilter tiers; structural-bias lookup; Kelly sizing; risk Protocol gating; tracker exit triggers; paper-fill sim. One integration test: full pipeline on fixture markets (paper) asserts intended orders. ~25-30 tests.

## Out of scope (YAGNI)
PM.US directional; OpenRouter; multi-agent ensemble; WebSocket; Streamlit dashboard.

## Effort
~450 lines new + ~80 modified, all behind directional.enabled. Live arb untouched.
