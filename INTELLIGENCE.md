# INTELLIGENCE.md — AI News Intelligence Layer

**Read this entire file before writing any code in `intelligence/`.**

---

## Purpose

The intelligence layer gives the bot awareness of *why* a market might be mispriced,
not just *that* it is. It does this by:

1. Extracting a search query from a market question (e.g. "Will the Fed raise rates in June 2026?" → `"Federal Reserve rate decision June 2026"`)
2. Fetching recent news headlines about that topic
3. Sending the market question + current odds + news headlines to Claude
4. Receiving a structured probability assessment and confidence score
5. Returning a `MarketSignal` that the arb engine can use to filter or boost positions

---

## Module Files

### `signal.py` — Dataclasses (scaffold this first)

No external dependencies. Pure Python dataclasses.

```python
@dataclass
class MarketSignal:
    market_id: str
    market_question: str
    current_yes_price: float         # Current market price for YES (0.0-1.0)
    ai_probability: float            # Claude's estimated true probability (0.0-1.0)
    confidence: float                # Claude's confidence in its estimate (0.0-1.0)
    direction: str                   # "agree" | "bullish" | "bearish" | "uncertain"
    reasoning: str                   # Short explanation from Claude (1-2 sentences)
    news_headlines: list[str]        # Headlines that informed the analysis
    timestamp: datetime
    cache_hit: bool = False          # Was this served from cache?

@dataclass
class SignalSummary:
    """Aggregated signal used by arb engine."""
    signal: MarketSignal | None
    should_filter: bool              # True = skip this arb opportunity
    should_boost: bool               # True = consider directional position
    adjusted_edge: float             # Original edge +/- signal adjustment
    reason: str                      # Human-readable explanation
```

---

### `cache.py` — TTL Cache

Simple in-memory dict cache. No Redis, no external deps.

```python
class SignalCache:
    def __init__(self, ttl_minutes: int = 10): ...
    def get(self, key: str) -> MarketSignal | None: ...
    def set(self, key: str, signal: MarketSignal) -> None: ...
    def clear_expired(self) -> int: ...  # returns count cleared
    def stats(self) -> dict: ...         # hit_rate, size, oldest_entry
```

Cache key = normalized topic string (lowercase, stripped). Do not use market_id as
key because the same real-world event may appear under different IDs across platforms.

---

### `topic_extractor.py` — Keyword Extraction

Converts a market question string into a concise search query for NewsAPI.

```python
class TopicExtractor:
    def extract(self, market_question: str) -> str:
        """
        Input:  "Will the Federal Reserve raise interest rates at the June 2026 FOMC meeting?"
        Output: "Federal Reserve interest rates June 2026 FOMC"

        Input:  "Will Donald Trump be indicted before November 2026?"
        Output: "Trump indictment 2026"

        Rules:
        - Remove question words: will, would, does, is, are, can, could, when, who, what
        - Remove filler: the, a, an, at, in, on, by, for, of, to, be, before, after
        - Keep: proper nouns, dates, numbers, domain-specific terms
        - Max 6 words in output
        - If Claude API is available, use it for extraction (more accurate)
        - Fall back to regex-based stripping if Claude is unavailable
        """
```

The Claude-based extraction path should use a short, cheap system prompt:
```
You extract a 4-6 word news search query from a prediction market question.
Respond with ONLY the search query, no punctuation, no explanation.
```

---

### `news_fetcher.py` — NewsAPI Integration

```python
class NewsFetcher:
    """
    Fetches recent news headlines from NewsAPI.org.
    Free tier: 500 requests/day, articles from last 30 days, English only.
    """
    def __init__(self, api_key: str, cache: SignalCache): ...

    async def fetch(
        self,
        topic: str,
        lookback_hours: int = 4,
        max_articles: int = 5,
        sources: list[str] | None = None,
    ) -> list[NewsArticle]: ...
```

```python
@dataclass
class NewsArticle:
    title: str
    description: str | None
    source: str
    published_at: datetime
    url: str
```

**NewsAPI endpoint to use:**
```
GET https://newsapi.org/v2/everything
  ?q={topic}
  &from={ISO8601 datetime lookback_hours ago}
  &sortBy=relevancy
  &language=en
  &pageSize={max_articles}
  &apiKey={NEWSAPI_KEY}
```

**Error handling:**
- 429 rate limit → log warning, return empty list, do NOT raise
- 401 bad key → log error once, disable fetcher for session
- Network timeout (5s default) → return empty list
- Empty results → return empty list (this is normal, not an error)

---

### `ai_analyzer.py` — Claude Integration

```python
class AIAnalyzer:
    """
    Sends market context + news to Claude and parses a structured signal.
    """
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 512,
        timeout_seconds: int = 8,
    ): ...

    async def analyze(
        self,
        market_question: str,
        current_yes_price: float,
        articles: list[NewsArticle],
    ) -> tuple[float, float, str]:
        """
        Returns: (ai_probability, confidence, reasoning)
        """
```

**System prompt for Claude:**
```
You are a prediction market analyst. You assess whether current market odds correctly
reflect recent news. You respond ONLY in valid JSON with no other text.

Response format:
{
  "probability": 0.72,       // your estimated true probability for YES (0.0-1.0)
  "confidence": 0.8,         // how confident you are in your estimate (0.0-1.0)
  "reasoning": "..."         // one sentence explanation
}

Rules:
- If news is irrelevant or insufficient, return probability close to current price and confidence below 0.5
- Never return confidence above 0.9 — prediction markets are inherently uncertain
- Base your estimate on the news provided, not your training data alone
- If you cannot assess the question, return {"probability": current_price, "confidence": 0.0, "reasoning": "Insufficient information"}
```

**User prompt template:**
```
Market question: {market_question}
Current YES price: {current_yes_price:.0%}

Recent news ({n} articles from last {lookback_hours} hours):
{formatted_headlines}

Based on this news, what is the true probability for YES?
```

**JSON parsing:**
- Strip markdown fences before parsing
- If JSON parse fails → return (current_yes_price, 0.0, "Parse error")
- If probability outside 0.0-1.0 → clamp
- If confidence outside 0.0-1.0 → clamp

---

## Signal Logic (how arb engine uses this)

The arb engine calls `IntelligenceEngine.evaluate(opportunity)` which returns a `SignalSummary`.

```python
class IntelligenceEngine:
    """Top-level coordinator. This is the only class imported by core/."""

    async def evaluate(
        self,
        market_id: str,
        market_question: str,
        current_yes_price: float,
        arb_edge: float,
    ) -> SignalSummary: ...
```

**Filter logic** (mode = "filter" or "both"):
- If `signal.direction == "bearish"` and we're about to buy YES → filter out
- If `signal.direction == "bullish"` and we're about to buy NO → filter out
- Only filter if `signal.confidence >= config.intelligence.min_confidence`

**Boost logic** (mode = "boost" or "both"):
- If `abs(signal.ai_probability - current_yes_price) > min_edge_boost`
  AND `signal.confidence >= min_confidence`
  → flag as boost opportunity with directional position suggestion

**Direction classification:**
```python
def classify_direction(ai_prob: float, market_price: float, confidence: float) -> str:
    delta = ai_prob - market_price
    if confidence < 0.5:
        return "uncertain"
    if delta > 0.05:
        return "bullish"   # AI thinks market underpriced YES
    if delta < -0.05:
        return "bearish"   # AI thinks market overpriced YES
    return "agree"
```

---

## Integration Point in `core/arb_engine.py`

Find the section where `Opportunity` objects are created. Add:

```python
# After opportunity is detected, before appending to results:
if self.intelligence_engine and self.config.intelligence.enabled:
    signal_summary = await self.intelligence_engine.evaluate(
        market_id=opportunity.market_id,
        market_question=opportunity.question,
        current_yes_price=opportunity.yes_price,
        arb_edge=opportunity.edge,
    )
    opportunity.signal = signal_summary

    if signal_summary.should_filter:
        logger.info(f"Intelligence filtered opportunity: {opportunity.market_id} — {signal_summary.reason}")
        continue
```

The `intelligence_engine` should be injected at construction time (dependency injection),
not instantiated inside the engine. This keeps it testable and optional.

---

## Testing Requirements

### `test_signal.py`
- Test `SignalSummary` construction
- Test `classify_direction` edge cases (delta exactly 0.05, confidence exactly 0.5)

### `test_cache.py`
- Test TTL expiry
- Test cache hit/miss
- Test `clear_expired` count

### `test_news_fetcher.py`
- Mock httpx calls
- Test 429 rate limit handling (returns empty list, no raise)
- Test empty results handling
- Test article parsing

### `test_ai_analyzer.py`
- Mock Anthropic API
- Test valid JSON parsing
- Test malformed JSON fallback
- Test probability clamping
- Test timeout handling

---

## Dependencies to Add to `requirements.txt`

```
httpx>=0.27.0          # already used in project, confirm version
newsapi-python>=0.2.7  # or use httpx directly (preferred — fewer deps)
```

Prefer using `httpx` directly for NewsAPI rather than adding the `newsapi-python` wrapper.
Less abstraction, consistent with project style.

---

## What NOT to Build (out of scope for Phase 1)

- Do not add Twitter/X or Reddit as news sources yet
- Do not add a persistent signal database (in-memory cache only for now)
- Do not implement the dashboard panel yet (Phase 3)
- Do not add retry logic for Claude API calls (timeout and skip is enough)
- Do not add sentiment scoring beyond what Claude provides
