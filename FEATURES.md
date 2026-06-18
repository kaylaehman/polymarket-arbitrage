# FEATURES.md — Planned Features Backlog

This file is for Claude Code. Each feature is self-contained with enough context
to implement without additional explanation. Work through them in order within each
category, but categories can be tackled in any sequence after Phase 1 is complete.

**Before starting any feature:** read `CLAUDE.md` for project context and constraints.
**All features must:** work with `intelligence.enabled: false`, not break simulation mode,
and be gated by a config flag so they can be disabled at runtime.

---

## Status Legend

* `[ ]` Not started
* `[~]` In progress
* `[x]` Complete

---

## Category 1 — Smarter Signals

### `[ ]` FEAT-01: Polymarket Whale Wallet Tracker

**What it does:**
Polymarket is on-chain — every trade is public on Polygon. Track a list of
historically profitable wallets and flag when they move into a market. A sharp
wallet taking a large position is a stronger signal than news alone.

**Where it lives:** `intelligence/whale_tracker.py`

**Implementation guide:**

1. Use the Polymarket CLOB API or Dune Analytics to pull recent trades for tracked wallets
2. Maintain a configurable list of wallet addresses in `config.yaml` under `intelligence.whales.addresses`
3. For each open opportunity, check if any tracked wallet has traded that market in the last N hours
4. Output a `WhaleSignal` that gets merged into `MarketSignal` in `intelligence/signal.py`

```python
@dataclass
class WhaleSignal:
    market_id: str
    wallet_address: str
    direction: str          # "YES" | "NO"
    size_usd: float
    timestamp: datetime
    wallet_historical_roi: float | None   # if known
```

**Config to add:**

```yaml
intelligence:
  whales:
    enabled: false
    addresses:
      - "0xabc..."          # add known sharp wallets here
    lookback_hours: 6
    min_trade_size_usd: 500  # ignore small trades
```

**Data source:** Polymarket CLOB API `/trades` endpoint — no additional API key needed,
trades are public. Alternatively query Dune Analytics (free tier) for historical wallet P&L
to build the initial list of sharp wallets to track.

**Integration point:** `intelligence/intelligence_engine.py` — merge whale signal
into `SignalSummary.adjusted_edge` if whale direction matches AI signal direction.
Boost confidence if both agree; flag conflict if they disagree.

---

### `[ ]` FEAT-02: Resolution Source Awareness

**What it does:**
Each Polymarket market has a defined resolution source and criteria (e.g. "Resolves YES
if the Federal Reserve raises rates at the June 2026 FOMC meeting per the official
Fed press release"). The AI analyzer currently doesn't know this. A market might have
bullish news but still resolve NO based on a technicality in the resolution criteria.

**Where it lives:** `intelligence/resolution_parser.py`

**Implementation guide:**

1. The Polymarket Gamma API returns a `description` field on each market containing resolution criteria
2. Parse and store this in `Market` model (or pass through to intelligence layer)
3. Include the resolution criteria in the Claude prompt as a separate field:

```
Resolution criteria: {resolution_description}

Given this specific criteria, does the news suggest YES or NO resolution?
```

4. Update `AIAnalyzer.analyze()` signature:

```python
async def analyze(
    self,
    market_question: str,
    current_yes_price: float,
    articles: list[NewsArticle],
    resolution_criteria: str | None = None,   # [NEW]
) -> tuple[float, float, str]: ...
```

**Why this matters:** Without resolution awareness, Claude might read "Fed signals
rate pause" as bullish for a "Will Fed raise rates?" YES position — but the resolution
criteria might say it needs to happen by a specific date or meeting, which the news
doesn't confirm.

---

### `[ ]` FEAT-03: Cross-Platform Crowd Disagreement Detection

**What it does:**
When Polymarket prices an event at 60% and Kalshi prices the same event at 40%,
that's not just an arb — something structural is causing the two crowds to disagree.
This is worth flagging separately as a "crowd disagreement" signal for human review,
even if the pure price gap doesn't meet arb threshold after fees.

**Where it lives:** `core/cross_platform_arb.py` (new opportunity type) + `intelligence/signal.py`

**Implementation guide:**

1. Add a new opportunity type `OpportunityType.CROWD_DISAGREEMENT`
2. Threshold: price delta > 15% after fees (larger than normal arb threshold)
3. When detected, run intelligence layer with both platform prices in the prompt:

```
Polymarket crowd: {poly_price:.0%} YES
Kalshi crowd: {kalshi_price:.0%} YES
Delta: {delta:.0%}

Which crowd do you think is more correct based on this news, and why?
```

4. Output to dashboard as a separate "Watch List" panel (Phase 3)
5. Do NOT auto-trade these — flag for human review only

**Config to add:**

```yaml
intelligence:
  crowd_disagreement:
    enabled: true
    min_delta: 0.15         # 15% price gap to flag
    alert_only: true        # never auto-trade, always human review
```

---

### `[ ]` FEAT-04: Additional News Sources

**What it does:**
Adds Reddit and optionally X (Twitter) as supplementary news sources alongside NewsAPI.
These are noisier but faster — breaking news often hits Reddit/X before wire services.

**Where it lives:** `intelligence/news_fetcher.py` (extend existing class)

**Implementation guide:**

Reddit (no API key needed for read-only):

* Use the Reddit JSON API: `https://www.reddit.com/r/politics+economics+worldnews/search.json?q={topic}&sort=new&limit=10`
* No authentication required for public subreddit search
* Filter to posts < `lookback_hours` old
* Extract: post title, top comment (if score > 100), subreddit

```python
class RedditFetcher:
    BASE_URL = "https://www.reddit.com"
    SUBREDDITS = ["politics", "economics", "worldnews", "news"]

    async def fetch(self, topic: str, lookback_hours: int = 4) -> list[NewsArticle]: ...
```

**Config to add:**

```yaml
intelligence:
  news:
    sources_enabled:
      newsapi: true
      reddit: false          # enable when ready
      # twitter: false       # future — requires API key
```

**Important:** Reddit posts are noisier than wire news. Treat Reddit articles with
lower weight in the Claude prompt — label them as `[Reddit]` in the headlines list
so Claude can calibrate accordingly.

---

## Category 2 — Risk and Position Management

### `[ ]` FEAT-05: Kelly Criterion Position Sizing

**What it does:**
Replaces flat position sizing with mathematically optimal sizing based on edge
and confidence. The Kelly formula: `f = (bp - q) / b` where `b` is the odds,
`p` is estimated win probability, `q = 1 - p`. A fractional Kelly (0.25x) is
used by default to reduce variance.

**Where it lives:** `core/risk_manager.py` (extend) + new `core/kelly.py`

**Implementation guide:**

```python
# core/kelly.py

def kelly_fraction(
    edge: float,            # net edge after fees (e.g. 0.07 for 7%)
    yes_price: float,       # current market price for YES
    ai_probability: float,  # Claude's estimated true probability
    confidence: float,      # Claude's confidence (0.0-1.0)
    fraction: float = 0.25, # fractional Kelly multiplier (default quarter-Kelly)
    max_fraction: float = 0.10,  # never bet more than 10% of bankroll on one market
) -> float:
    """
    Returns the fraction of available capital to deploy on this opportunity.

    Uses AI probability if confidence >= 0.6, otherwise falls back to
    market-implied probability adjusted by edge.
    """
    if confidence >= 0.6:
        p = ai_probability
    else:
        # No AI signal — use edge to imply probability
        p = yes_price + edge

    q = 1 - p
    b = (1 / yes_price) - 1   # implied odds

    if b <= 0 or p <= 0:
        return 0.0

    raw_kelly = (b * p - q) / b
    fractional = raw_kelly * fraction
    return max(0.0, min(fractional, max_fraction))
```

**Integration in `core/execution.py`:**

```python
from core.kelly import kelly_fraction

# Replace flat order size with:
if config.trading.kelly_enabled and opportunity.signal:
    fraction = kelly_fraction(
        edge=opportunity.edge,
        yes_price=opportunity.yes_price,
        ai_probability=opportunity.signal.signal.ai_probability,
        confidence=opportunity.signal.signal.confidence,
        fraction=config.trading.kelly_fraction,
    )
    order_size = fraction * portfolio.available_capital
    order_size = max(config.trading.min_order_size, min(order_size, config.trading.max_order_size))
else:
    order_size = config.trading.default_order_size
```

**Config to add:**

```yaml
trading:
  kelly_enabled: false        # enable after backtesting confirms signal quality
  kelly_fraction: 0.25        # quarter-Kelly for reduced variance
  min_order_size: 5           # floor even if Kelly says less
  max_order_size: 100         # ceiling even if Kelly says more
```

**Warning:** Do NOT enable Kelly sizing until FEAT-09 (signal database) has
accumulated enough signal history to validate that AI probability estimates are
actually calibrated. Miscalibrated probabilities + Kelly sizing = rapid capital loss.

---

### `[ ]` FEAT-06: Correlated Market Exposure Limits

**What it does:**
Prevents over-exposure to a single real-world theme. If you're holding positions
in "Fed raises rates June 2026" and "Inflation above 3% Q2 2026", those are
correlated — both go wrong if the economy shifts unexpectedly.

**Where it lives:** `core/risk_manager.py` (extend) + `intelligence/correlation.py`

**Implementation guide:**

```python
# intelligence/correlation.py

class CorrelationDetector:
    """
    Groups open positions by real-world theme using Claude.
    Called periodically (every 5 min), not on every trade.
    """
    THEMES = [
        "federal_reserve_rates",
        "us_elections",
        "crypto_prices",
        "inflation_cpi",
        "geopolitical_conflict",
        "sports",
        "entertainment",
        "other",
    ]

    async def classify_theme(self, market_question: str) -> str:
        """Ask Claude to classify market into one of THEMES."""
        ...

    def get_theme_exposure(self, portfolio: Portfolio) -> dict[str, float]:
        """Returns total $ exposure per theme."""
        ...
```

**Risk manager integration:**

```python
# In risk_manager.py
MAX_THEME_EXPOSURE_USD = config.risk.max_theme_exposure  # e.g. 200

def check_correlation_limit(self, opportunity, theme: str) -> bool:
    current_exposure = self.theme_exposure.get(theme, 0)
    return current_exposure + opportunity.size <= MAX_THEME_EXPOSURE_USD
```

**Config to add:**

```yaml
risk:
  max_theme_exposure: 200     # max total $ in any single real-world theme
  correlation_check_enabled: false
```

---

### `[ ]` FEAT-07: Time-Decay Position Discounting

**What it does:**
Markets resolving soon are riskier — less time for price correction to work.
Discount expected edge and reduce position size as resolution date approaches.

**Where it lives:** `core/arb_engine.py` (small addition)

**Implementation guide:**

```python
# core/arb_engine.py — add to opportunity evaluation

def time_decay_multiplier(resolution_date: datetime | None) -> float:
    """
    Returns a multiplier (0.0-1.0) that discounts edge based on time to resolution.

    > 7 days: 1.0 (no discount)
    2-7 days: 0.75
    24-48 hrs: 0.5
    < 24 hrs:  0.25 (very risky, avoid unless edge is huge)
    """
    if resolution_date is None:
        return 1.0
    hours_remaining = (resolution_date - datetime.utcnow()).total_seconds() / 3600
    if hours_remaining > 168:   # 7 days
        return 1.0
    elif hours_remaining > 48:
        return 0.75
    elif hours_remaining > 24:
        return 0.5
    else:
        return 0.25

# Apply before threshold check:
adjusted_edge = raw_edge * time_decay_multiplier(opportunity.resolution_date)
if adjusted_edge < config.trading.min_edge:
    continue
```

The `resolution_date` field needs to be parsed from the Polymarket Gamma API
response (`end_date_iso` field) and stored on the `Market` model.

**Config to add:**

```yaml
trading:
  time_decay_enabled: true
  skip_if_resolves_within_hours: 12   # hard skip regardless of edge
```

---

## Category 3 — Infrastructure

### `[ ]` FEAT-08: Telegram Alerts

**What it does:**
Sends Telegram notifications for key bot events: new high-confidence opportunity,
intelligence filter triggered, daily P&L summary, kill switch triggered, errors.

**Where it lives:** `utils/telegram.py`

**Implementation guide:**

Uses the Telegram Bot API — you already have a bot token from the homelab agent project.
Reuse the same bot or create a new one with BotFather.

```python
# utils/telegram.py

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str): ...

    async def send(self, message: str) -> None:
        """POST to https://api.telegram.org/bot{token}/sendMessage"""
        ...

    async def send_opportunity(self, opp: Opportunity) -> None:
        """Formatted opportunity alert with edge, signal, and market link."""
        ...

    async def send_daily_summary(self, portfolio: Portfolio) -> None:
        """End-of-day P&L, opportunities found, trades executed, signals fired."""
        ...

    async def send_error(self, context: str, error: Exception) -> None:
        """Error alert with context."""
        ...
```

**Message format for opportunities:**

```
🎯 Arb Opportunity Detected
Market: {market_question[:80]}
Edge: {edge:.1%} (after fees)
Platform: {buy_platform} → {sell_platform}
AI Signal: {signal.direction} ({signal.confidence:.0%} confidence)
Reason: {signal.reasoning}
Link: {polymarket_url}
```

**Config to add:**

```yaml
notifications:
  telegram:
    enabled: false
    bot_token: ""             # or set via TELEGRAM_BOT_TOKEN env var
    chat_id: ""               # or set via TELEGRAM_CHAT_ID env var
    min_edge_to_alert: 0.05   # only alert on 5%+ edge opportunities
    daily_summary_hour: 20    # 8pm local time
    alert_on_filter: true     # notify when intelligence filters an opportunity
    alert_on_error: true
```

**Environment variables:**

```bash
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

---

### `[ ]` FEAT-09: Signal Database (SQLite)

**What it does:**
Persists every `MarketSignal` and `Opportunity` to SQLite so you can measure
whether AI signals are actually predictive over time. This is the foundation
for backtesting and Kelly sizing validation.

**Where it lives:** `utils/signal_db.py`

**Implementation guide:**

Use Python's built-in `sqlite3` — no new dependencies.

```python
# utils/signal_db.py

class SignalDB:
    """
    Stores signals and opportunities for post-hoc analysis.
    Schema is append-only — never update or delete rows.
    """

    def __init__(self, db_path: str = "data/signals.db"): ...

    def init_schema(self) -> None:
        """Create tables if not exist."""
        ...

    def log_signal(self, signal: MarketSignal) -> None: ...
    def log_opportunity(self, opp: Opportunity) -> None: ...
    def log_outcome(self, market_id: str, resolved_yes: bool) -> None: ...

    def get_signal_accuracy(
        self,
        min_confidence: float = 0.65,
        lookback_days: int = 30,
    ) -> dict:
        """
        Returns: {
            total_signals: int,
            correct: int,
            accuracy: float,
            avg_confidence: float,
            calibration_error: float,   # how far confidence is from accuracy
        }
        """
        ...
```

**Tables:**

```sql
CREATE TABLE signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    platform TEXT,
    current_yes_price REAL,
    ai_probability REAL,
    confidence REAL,
    direction TEXT,
    reasoning TEXT,
    news_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    opportunity_type TEXT,
    raw_edge REAL,
    adjusted_edge REAL,
    was_filtered INTEGER,        -- 0 or 1
    filter_reason TEXT,
    signal_id INTEGER REFERENCES signals(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    resolved_yes INTEGER,        -- 0 or 1
    resolution_date TIMESTAMP,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Market outcome resolution:**
Polymarket publishes resolution via the Gamma API when a market closes.
Run a background job every hour to check for newly resolved markets and
log outcomes for any market_id in the signals table.

**Config to add:**

```yaml
database:
  enabled: true
  path: "data/signals.db"
  log_signals: true
  log_opportunities: true
  auto_log_outcomes: true      # poll for resolutions hourly
```

---

### `[ ]` FEAT-10: Backtester Extension

**What it does:**
Extends the existing `utils/backtest.py` to replay historical signals from
the database and measure whether AI-informed trades outperformed pure arb.

**Where it lives:** `utils/backtest.py` (extend existing) + `utils/signal_db.py` (FEAT-09 required)

**Prerequisite:** FEAT-09 must be complete and have at least 2 weeks of signal data.

**Implementation guide:**

Add a new `BacktestAnalyzer` class that reads from `signals.db`:

```python
class BacktestAnalyzer:
    def __init__(self, db: SignalDB): ...

    def run(
        self,
        start_date: datetime,
        end_date: datetime,
        strategy: str = "intelligence_filter",   # "baseline" | "intelligence_filter" | "intelligence_boost"
        min_confidence: float = 0.65,
        initial_capital: float = 1000.0,
    ) -> BacktestResult: ...

@dataclass
class BacktestResult:
    strategy: str
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    roi: float
    sharpe_ratio: float
    max_drawdown: float
    filtered_by_intelligence: int    # how many the AI filtered out
    filter_accuracy: float           # of filtered trades, how many would have lost
    boost_trades: int                # directional trades from AI signal alone
    boost_accuracy: float
```

Add a CLI command:

```bash
python -m utils.backtest --start 2026-05-01 --end 2026-06-01 --strategy intelligence_filter
```

---

### `[ ]` FEAT-11: Docker Deployment

**What it does:**
Packages the bot for deployment on the homelab `docker-services` VM
(192.168.20.9) alongside other services. Accessible via the Portainer dashboard.

**Where it lives:** `Dockerfile` + `docker-compose.yml` (repo root)

**Implementation guide:**

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install deps first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Create data dir for SQLite
RUN mkdir -p data logs

EXPOSE 8000

CMD ["python", "run_with_dashboard.py"]
```

```yaml
# docker-compose.yml
version: "3.8"

services:
  polymarket-bot:
    build: .
    container_name: polymarket-bot
    ports:
      - "8124:8000"           # pick free port on homelab
    environment:
      - NEWSAPI_KEY=${NEWSAPI_KEY}
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - ANTHROPIC_BASE_URL=http://192.168.20.9:3456   # OpenClaw
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
      - TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - polymarket_data:/app/data
      - polymarket_logs:/app/logs
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  polymarket_data:
  polymarket_logs:
```

Add a `/health` endpoint to `dashboard/server.py`:

```python
@app.get("/health")
async def health():
    return {"status": "ok", "uptime_seconds": bot.uptime}
```

Add to Homepage dashboard (home.kaylas.systems) after deployment:

```yaml
# services.yaml entry
- name: Polymarket Bot
  href: http://192.168.20.9:8124
  icon: mdi-chart-line
  description: Prediction market arb bot
```

---

## Implementation Order (Recommended)

After Phase 1 (intelligence scaffold) and Phase 2 (core hooks) are complete:

```
FEAT-09 → FEAT-02 → FEAT-05 → FEAT-07 → FEAT-08 → FEAT-11
  DB         Resolution  Kelly     Time-decay  Telegram  Docker
  (measure   (better     (sizing   (risk)      (alerts)  (deploy)
  everything) prompts)   after DB)
```

Then once you have real signal data and can validate quality:

```
FEAT-01 → FEAT-03 → FEAT-06 → FEAT-04 → FEAT-10
  Whales    Crowd     Correlation Reddit    Backtest
            disagree  limits      sources   analysis
```

---

## Notes for Claude Code

* Each feature is independent unless marked with **Prerequisite**
* Always run `pytest tests/ -v` after completing a feature before moving to the next
* Features touching `core/` must follow the additive-only rules in `CORE_HOOKS.md`
* Features adding config keys must also update `utils/config_loader.py` with defaults
* Every new module needs corresponding tests in `tests/`
* No feature should require `intelligence.enabled: true` to boot — always default false
