# CORE_HOOKS.md — Modifying Existing Core Files

**Read this before touching anything in `core/`.**

The base repo's core files are working and tested. Our goal is to add intelligence
signals with the absolute minimum surface area of changes. Do not refactor existing
logic — only add hooks.

---

## Rule: Additive Only

Every change to an existing file must be:
- **Optional at runtime** — controlled by `config.intelligence.enabled`
- **Non-breaking** — if intelligence layer throws, core continues unchanged
- **Backwards compatible** — simulation mode must still work with zero config changes

---

## `core/cross_platform_arb.py` — Add signal field to Opportunity

Find the `Opportunity` dataclass (or whatever structure represents a detected arb).
Add one optional field:

```python
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from intelligence.signal import SignalSummary

@dataclass
class Opportunity:
    # ... existing fields unchanged ...

    # [NEW] Intelligence signal — None if intelligence disabled or timed out
    signal: "SignalSummary | None" = field(default=None, repr=False)
```

Use `TYPE_CHECKING` guard to avoid circular imports. The intelligence module
should never import from core — only core imports from intelligence.

---

## `core/arb_engine.py` — Inject and call intelligence engine

### Constructor change

```python
class ArbEngine:
    def __init__(
        self,
        config,
        risk_manager,
        portfolio,
        intelligence_engine=None,   # [NEW] optional, injected from main.py
    ):
        self.intelligence_engine = intelligence_engine
        # ... rest unchanged ...
```

### Detection loop change

Immediately after an opportunity is appended to results (or before, to filter):

```python
# [NEW BLOCK — wrap entirely in try/except]
if self.intelligence_engine is not None and config.intelligence.enabled:
    try:
        import asyncio
        signal_summary = asyncio.run_coroutine_threadsafe(
            self.intelligence_engine.evaluate(
                market_id=opp.market_id,
                market_question=opp.question,
                current_yes_price=opp.yes_price,
                arb_edge=opp.edge,
            ),
            self._event_loop,   # pass in the running loop from data_feed
        ).result(timeout=config.intelligence.claude.timeout_seconds + 1)

        opp.signal = signal_summary

        if signal_summary.should_filter:
            logger.info(
                f"[Intelligence] Filtered {opp.market_id}: {signal_summary.reason}"
            )
            continue  # skip appending this opportunity

    except Exception as e:
        logger.warning(f"[Intelligence] Signal failed for {opp.market_id}: {e}")
        # opp.signal stays None, opportunity proceeds normally
# [END NEW BLOCK]
```

**Important:** If the arb engine is already async, use `await` instead of
`run_coroutine_threadsafe`. Check the existing `detect_opportunities` signature first.

---

## `main.py` — Wire up intelligence engine

In the section where `ArbEngine` is constructed, add:

```python
from intelligence.ai_analyzer import AIAnalyzer
from intelligence.news_fetcher import NewsFetcher
from intelligence.cache import SignalCache
from intelligence.ai_analyzer import AIAnalyzer
from intelligence.intelligence_engine import IntelligenceEngine

intelligence_engine = None
if config.intelligence.enabled:
    _cache = SignalCache(ttl_minutes=config.intelligence.news.cache_ttl_minutes)
    _fetcher = NewsFetcher(api_key=os.getenv("NEWSAPI_KEY"), cache=_cache)
    _analyzer = AIAnalyzer(
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        model=config.intelligence.claude.model,
        max_tokens=config.intelligence.claude.max_tokens,
        timeout_seconds=config.intelligence.claude.timeout_seconds,
    )
    intelligence_engine = IntelligenceEngine(
        fetcher=_fetcher,
        analyzer=_analyzer,
        config=config.intelligence,
    )
    logger.info("[Intelligence] Engine initialized")

arb_engine = ArbEngine(
    config=config,
    risk_manager=risk_manager,
    portfolio=portfolio,
    intelligence_engine=intelligence_engine,  # [NEW]
)
```

---

## `config.yaml` — New section to append

Add this block at the end of the existing config. Do not modify existing keys.

```yaml
# ── Intelligence Layer ────────────────────────────────────────────────────────
intelligence:
  enabled: false              # Set to true once NEWSAPI_KEY and ANTHROPIC_API_KEY are set
  mode: "filter"              # "filter" | "boost" | "both"
  min_confidence: 0.65
  min_edge_boost: 0.03
  max_position_boost: 10

  news:
    lookback_hours: 4
    max_articles: 5
    cache_ttl_minutes: 10
    sources: []

  claude:
    model: "claude-sonnet-4-6"
    max_tokens: 512
    timeout_seconds: 8
```

---

## `utils/config_loader.py` — Add intelligence config parsing

Find where config sections are parsed into objects. Add:

```python
@dataclass
class IntelligenceNewsConfig:
    lookback_hours: int = 4
    max_articles: int = 5
    cache_ttl_minutes: int = 10
    sources: list = field(default_factory=list)

@dataclass
class IntelligenceClaudeConfig:
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 512
    timeout_seconds: int = 8

@dataclass
class IntelligenceConfig:
    enabled: bool = False
    mode: str = "filter"
    min_confidence: float = 0.65
    min_edge_boost: float = 0.03
    max_position_boost: float = 10.0
    news: IntelligenceNewsConfig = field(default_factory=IntelligenceNewsConfig)
    claude: IntelligenceClaudeConfig = field(default_factory=IntelligenceClaudeConfig)
```

Add `intelligence: IntelligenceConfig` to the top-level config dataclass with
`default_factory=IntelligenceConfig` so existing configs without the new section
still parse without error.

---

## Files to NOT Touch

- `polymarket_client/api.py` — no changes needed
- `polymarket_client/models.py` — no changes needed
- `kalshi_client/api.py` — no changes needed
- `kalshi_client/models.py` — no changes needed
- `core/execution.py` — no changes needed
- `core/risk_manager.py` — no changes needed
- `core/portfolio.py` — no changes needed
- `dashboard/server.py` — Phase 3 only
- `dashboard/integration.py` — Phase 3 only

---

## Checklist Before Opening a PR

- [ ] `python run_with_dashboard.py` works with `intelligence.enabled: false` (default)
- [ ] `python run_with_dashboard.py` works with `data_mode: "simulation"`
- [ ] `pytest tests/ -v` all pass
- [ ] `pytest tests/intelligence/ -v` all pass with mocked APIs
- [ ] No new imports in core files except inside `if TYPE_CHECKING:` blocks or guarded by `intelligence_engine is not None`
- [ ] All intelligence API calls wrapped in try/except
- [ ] No hardcoded API keys anywhere
