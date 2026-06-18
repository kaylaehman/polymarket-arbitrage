# CLAUDE.md — Polymarket Arbitrage Bot (Forked + Extended)

## Project Overview

This is a fork of [ImMike/polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage), a Python bot that
detects cross-platform price discrepancies between Polymarket and Kalshi prediction markets.

**Our extension adds an `intelligence/` layer** — an AI news reading agent that ingests recent headlines,
analyzes them against open market questions, and produces probability adjustments that inform trading decisions.
The goal is to catch mispricings *before* the market corrects them, not just react to price gaps.

---

## What This Bot Does

1. Fetches live orderbooks from Polymarket (CLOB API on Polygon) and Kalshi (REST API, CFTC-regulated)
2. Matches equivalent markets across platforms using text similarity
3. Detects two arbitrage types:
   - **Cross-platform arb**: same event priced differently on each platform
   - **Bundle arb**: YES + NO prices on one platform don't sum to ~$1.00
4. **(New)** Reads recent news for each market topic and asks Claude to assess whether current odds reflect reality
5. Flags or filters opportunities based on AI signal confidence
6. Executes trades (dry run by default) and tracks P&L

---

## Repository Structure

```
polymarket-arbitrage/
│
├── CLAUDE.md                        ← YOU ARE HERE
├── main.py                          ← Entry point (bot only)
├── run_with_dashboard.py            ← Entry point (bot + live UI)
├── config.yaml                      ← All runtime config (edit this)
├── requirements.txt                 ← Python dependencies
│
├── polymarket_client/               ← Polymarket API wrapper
│   ├── api.py                       ← REST + WebSocket, Gamma API for market discovery
│   └── models.py                    ← OrderBook, Market dataclasses
│
├── kalshi_client/                   ← Kalshi API wrapper
│   ├── api.py                       ← REST API (no auth needed for reads)
│   └── models.py                    ← Kalshi market dataclasses
│
├── core/                            ← Trading logic (DO NOT break interfaces here)
│   ├── data_feed.py                 ← Coordinates live data from both clients
│   ├── arb_engine.py                ← Single-platform bundle arb detection
│   ├── cross_platform_arb.py        ← Cross-platform opportunity detection
│   ├── execution.py                 ← Order placement (dry run / live)
│   ├── risk_manager.py              ← Position limits, loss limits, kill switch
│   └── portfolio.py                 ← Position tracking and P&L
│
├── intelligence/                    ← [NEW] AI news reading layer (scaffold this)
│   ├── INTELLIGENCE.md              ← Module spec (read before touching this dir)
│   ├── news_fetcher.py              ← Fetches headlines from NewsAPI + web search
│   ├── ai_analyzer.py               ← Sends market context + news to Claude API
│   ├── signal.py                    ← MarketSignal dataclass and signal aggregator
│   ├── cache.py                     ← TTL cache to avoid redundant API calls
│   └── topic_extractor.py           ← Extracts search keywords from market questions
│
├── dashboard/                       ← FastAPI web UI
│   ├── server.py                    ← API endpoints + embedded HTML
│   └── integration.py               ← Bridge between bot state and dashboard
│
├── utils/
│   ├── config_loader.py             ← Parses config.yaml
│   ├── logging_utils.py             ← Colored console logging
│   └── backtest.py                  ← Backtesting engine
│
└── tests/
    ├── test_arb_engine.py
    ├── test_risk_manager.py
    ├── test_portfolio.py
    └── intelligence/                ← [NEW] Tests for intelligence layer
        ├── test_news_fetcher.py
        ├── test_ai_analyzer.py
        └── test_signal.py
```

---

## What to Scaffold First

Claude Code should scaffold in this order. Do not skip ahead.

### Phase 1 — Intelligence Module (primary task)
Read `intelligence/INTELLIGENCE.md` before writing any code in `intelligence/`.
Scaffold these files in order:

1. `intelligence/signal.py` — dataclasses only, no external deps
2. `intelligence/cache.py` — simple TTL dict cache
3. `intelligence/topic_extractor.py` — keyword extraction from market question strings
4. `intelligence/news_fetcher.py` — NewsAPI integration
5. `intelligence/ai_analyzer.py` — Claude API integration
6. `tests/intelligence/` — unit tests for each module

### Phase 2 — Hook into Core
Read `core/CORE_HOOKS.md` before modifying any existing core files.
Modify these files minimally:

1. `core/cross_platform_arb.py` — add optional `intelligence_signal` field to `Opportunity`
2. `core/arb_engine.py` — call intelligence layer and attach signal before returning opportunities
3. `config.yaml` — add `intelligence:` section

### Phase 3 — Dashboard Extension
Add an "AI Signals" panel to the existing dashboard showing recent signals and their impact.

---

## Key Constraints

- **Do not break simulation mode.** The bot must still run fully with `data_mode: "simulation"` and
  `intelligence_enabled: false` in config. Intelligence layer is always optional/additive.
- **All intelligence calls are async.** Use `asyncio` consistently. Do not block the arb detection loop.
- **Cache aggressively.** NewsAPI has rate limits. Default TTL for news cache is 10 minutes per market topic.
- **Signal is advisory, not mandatory.** The arb engine proceeds even if the intelligence layer fails or times out.
  Wrap all intelligence calls in try/except and log warnings, never raise to caller.
- **Dry run by default.** `trading_mode: "dry_run"` in config.yaml. Never change this default.
- **No hardcoded API keys.** All secrets via environment variables. See Environment Variables section below.

---

## Environment Variables

```bash
# Polymarket (required for live trading only)
POLYMARKET_API_KEY=
POLYMARKET_PRIVATE_KEY=

# Kalshi (required for live trading only)
KALSHI_API_KEY=

# Intelligence layer (required if intelligence_enabled: true)
ANTHROPIC_API_KEY=           # or route through OpenClaw at localhost:3456
NEWSAPI_KEY=                 # free tier: newsapi.org, 500 req/day

# Optional: override Claude endpoint (for OpenClaw proxy)
ANTHROPIC_BASE_URL=http://localhost:3456  # comment out to use api.anthropic.com directly
```

---

## Config Reference (intelligence section to add)

```yaml
intelligence:
  enabled: true
  mode: "filter"              # "filter" | "boost" | "both"
                              # filter: skip arb if AI disagrees with cheap side
                              # boost: flag directional positions from news alone
                              # both: do both

  min_confidence: 0.65        # AI confidence threshold to act on signal (0.0-1.0)
  min_edge_boost: 0.03        # Additional edge required for a "boost" directional trade
  max_position_boost: 10      # Max $ per AI-boosted directional position

  news:
    lookback_hours: 4         # How far back to fetch news (max 24 for free NewsAPI tier)
    max_articles: 5           # Articles to send to Claude per market
    cache_ttl_minutes: 10     # Don't re-fetch news for same topic within this window
    sources: []               # Leave empty to use NewsAPI defaults, or e.g. ["reuters", "bbc-news"]

  claude:
    model: "claude-sonnet-4-6"
    max_tokens: 512
    timeout_seconds: 8        # If Claude doesn't respond in time, skip signal and continue
```

---

## Running the Bot

```bash
# Install deps
pip install -r requirements.txt

# Simulation mode (safe, no real money, no API keys needed)
python run_with_dashboard.py

# Real data, dry run (reads live markets, does not trade)
# Set data_mode: "real" and trading_mode: "dry_run" in config.yaml
python run_with_dashboard.py

# Live trading (requires all env vars set, use with extreme caution)
# Set trading_mode: "live" in config.yaml
python run_with_dashboard.py
```

Dashboard available at `http://localhost:8000`

---

## Testing

```bash
# All tests
pytest tests/ -v

# Intelligence module only
pytest tests/intelligence/ -v

# With coverage
pytest tests/ --cov=core --cov=intelligence
```

---

## Development Notes

- Python 3.10+
- All API calls use `httpx` (async) — do not introduce `requests` as a dep
- Dataclasses preferred over Pydantic for internal models (keep it lightweight)
- FastAPI dashboard uses vanilla JS — do not introduce frontend build tooling
- The existing market matcher uses fuzzy string similarity — topic_extractor.py
  should produce *search query strings*, not try to re-implement matching logic
- OpenClaw proxy (localhost:3456) is available on the homelab and can be used
  instead of calling api.anthropic.com directly — set ANTHROPIC_BASE_URL accordingly
