# Directional Trading Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Kalshi-only "directional" trading mode that takes positions on individual markets from signals (pure-math Safe Compounder + AI-directional), with or without cross-platform matches, behind a flag, paper-first.

**Architecture:** Standalone `core/directional/` package launched as one independent asyncio task; reuses `kalshi_client`, `intelligence/`, `core/kelly.py`, `core/risk_manager.py`; never touches the live arb loop. Two strategies under one engine; per-strategy paper/live; separate risk caps; SQLite persistence; integrated dashboard section.

**Tech Stack:** Python 3.12 async, httpx, cryptography (existing), pytest + pytest-asyncio. No new deps.

## Global Constraints
- MUST NOT disrupt the live Kalshi-native arb (real money). The directional task is `create_task`'d ONLY when `directional.enabled`; its loop body is wrapped in try/except; it shares only read-mostly services.
- Paper-first: each strategy has `mode: paper|live`; live requires explicit per-strategy flag.
- Separate caps: `directional_max_total_exposure=30`, `directional_max_position=8`, `directional_max_open=4` (USD). Global kill switch / daily-loss / drawdown still apply.
- Reuse existing: `kalshi_client/api.py`, `intelligence/intelligence_engine.py`, `core/kelly.py::kelly_fraction(edge, yes_price, ai_probability, confidence, fraction=0.25, max_fraction=0.10)`, `core/risk_manager.py::RiskManager.check_order(order)`.
- All tests mock `kalshi_client` + intelligence — NO live API calls in tests.
- Reference ports (MIT): `/tmp/pma-jbecker/src/analysis/kalshi/util/categories.py`, `/tmp/kalshi-ai-bot/src/strategies/safe_compounder.py`, `/tmp/kalshi-ai-bot/src/utils/edge_filter.py`, `/tmp/kalshi-ai-bot/src/utils/stop_loss_calculator.py`, `/tmp/kalshi-ai-bot/src/jobs/ingest.py`.
- Run tests inside the container: `ssh docker-services 'docker cp <files> polymarket-arb:/app/... && docker exec -w /app polymarket-arb python -m pytest <path> -q'` OR set up a local venv. Each task commits with `git -C ~/docker/polymarket-arbitrage`.

## File Structure
- `utils/kalshi_categories.py` — ported category taxonomy (event_ticker prefix → category).
- `utils/structural_bias.py` — repo#1 findings as params + `structural_score(price, side, category)`.
- `utils/edge_filter.py` — confidence-tiered edge thresholds (ported).
- `core/directional/__init__.py`
- `core/directional/models.py` — DirectionalCandidate, DirectionalOrder, DirectionalPosition.
- `core/directional/scanner.py` — KalshiMarketScanner.
- `core/directional/strategies/base.py` — Strategy ABC.
- `core/directional/strategies/safe_compounder.py` — pure-math NO-side.
- `core/directional/strategies/ai_directional.py` — intelligence + edge filter + bias.
- `core/directional/decider.py` — Kelly size + risk gate.
- `core/directional/executor.py` — paper/live placement.
- `core/directional/tracker.py` — exits + resolution sweep (+ StopLossCalculator port).
- `core/directional/store.py` — SQLite persistence (directional_positions/signals).
- `core/directional/engine.py` — DirectionalEngine loop.
- Modify: `core/risk_manager.py` (Order Protocol + directional caps + directional exposure tracking), `utils/config_loader.py` (DirectionalConfig), `config.yaml` (directional block), `run_with_dashboard.py` (launch task), `dashboard/server.py` (/api/directional + HTML panel).
- Tests: `tests/directional/test_*.py`.

---

### Task 1: Port the Kalshi category taxonomy

**Files:**
- Create: `utils/kalshi_categories.py` (copy verbatim from `/tmp/pma-jbecker/src/analysis/kalshi/util/categories.py`)
- Test: `tests/directional/test_kalshi_categories.py`

**Interfaces:**
- Produces: `categorize(event_ticker: str) -> str` returning a category like "Sports"/"Politics"/"Finance"/"Crypto"/"Other". (If the ported file's public function has a different name, re-export it as `categorize`.)

- [ ] **Step 1: Copy the file**
```bash
cp /tmp/pma-jbecker/src/analysis/kalshi/util/categories.py ~/docker/polymarket-arbitrage/utils/kalshi_categories.py
# Inspect its public API; if the lookup fn isn't named categorize(), add: `def categorize(t): return <existing_fn>(t)`
```
- [ ] **Step 2: Write the failing test**
```python
# tests/directional/test_kalshi_categories.py
from utils.kalshi_categories import categorize
def test_known_prefixes_map_to_categories():
    assert categorize("KXNFLGAME-26SEP-KC") == "Sports"
    assert categorize("KXSENATE-26NOV-R") == "Politics"
    assert categorize("KXUNKNOWNXYZ-99") in ("Other", "Unknown", "")
```
- [ ] **Step 3: Run — adjust expected category strings to match the ported file's taxonomy** (read the file's mapping; the test documents real outputs).
Run: `python -m pytest tests/directional/test_kalshi_categories.py -q` → PASS
- [ ] **Step 4: Commit**
```bash
git add utils/kalshi_categories.py tests/directional/test_kalshi_categories.py
git commit -m "feat(directional): port Kalshi category taxonomy (MIT)"
```

---

### Task 2: Structural-bias parameters + scorer

**Files:**
- Create: `utils/structural_bias.py`
- Test: `tests/directional/test_structural_bias.py`

**Interfaces:**
- Produces: `structural_score(price: float, side: str, category: str) -> float` — a small additive edge score (positive favors the trade). `price` is the contract's own price 0..1; `side` is "YES"|"NO"; `category` from `categorize()`.

- [ ] **Step 1: Write the failing test**
```python
from utils.structural_bias import structural_score
def test_longshot_no_bias_positive():
    # NO at a longshot YES price is favored (repo#1 longshot bias)
    assert structural_score(price=0.10, side="NO", category="Sports") > 0
def test_yes_longshot_disfavored():
    assert structural_score(price=0.10, side="YES", category="Sports") <= 0
def test_category_edge_sports_gt_finance():
    assert structural_score(0.10, "NO", "Sports") > structural_score(0.10, "NO", "Finance")
```
- [ ] **Step 2: Run to verify FAIL** (`ModuleNotFoundError`).
- [ ] **Step 3: Implement**
```python
# utils/structural_bias.py
"""Structural-bias parameters from Jon-Becker/prediction-market-analysis (MIT).
Markets are well-calibrated overall, so these are SECONDARY tiebreakers, not a
primary signal. Magnitudes are conservative approximations of the paper's figures
and should be re-validated against fresh data before sizing up."""

# NO-minus-YES EV advantage (cents) by YES-price bucket; positive => NO favored.
_YES_NO_EV_BIAS = {5: 8.0, 10: 5.0, 20: 3.0, 50: 0.0, 80: -2.0, 90: -3.0}
# Maker/NO excess edge by category (fraction); Sports largest, Finance smallest.
_CATEGORY_MAKER_EDGE = {"Sports": 0.04, "Politics": 0.02, "Entertainment": 0.02,
                        "Crypto": 0.01, "Finance": 0.005}

def _interp_bias(yes_price_cents: float) -> float:
    pts = sorted(_YES_NO_EV_BIAS.items())
    if yes_price_cents <= pts[0][0]: return pts[0][1]
    if yes_price_cents >= pts[-1][0]: return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= yes_price_cents <= x1:
            t = (yes_price_cents - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return 0.0

def structural_score(price: float, side: str, category: str) -> float:
    yes_cents = price * 100 if side == "YES" else (1 - price) * 100
    bias_cents = _interp_bias(yes_cents)              # >0 => NO favored at this YES price
    directional = (bias_cents if side == "NO" else -bias_cents) / 100.0
    cat_edge = _CATEGORY_MAKER_EDGE.get(category, 0.005)
    return directional + (cat_edge if side == "NO" else 0.0)
```
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(directional): structural-bias scorer (repo#1 findings)"`

---

### Task 3: Edge filter (ported)

**Files:**
- Create: `utils/edge_filter.py` (adapt `/tmp/kalshi-ai-bot/src/utils/edge_filter.py`)
- Test: `tests/directional/test_edge_filter.py`

**Interfaces:**
- Produces: `passes_edge(confidence: float, edge: float) -> bool` — tiered: conf≥0.8 needs edge≥0.03; ≥0.6 needs ≥0.05; else ≥0.08; conf<0.35 always False.

- [ ] **Step 1: Failing test**
```python
from utils.edge_filter import passes_edge
def test_tiers():
    assert passes_edge(0.85, 0.04) is True
    assert passes_edge(0.85, 0.02) is False
    assert passes_edge(0.65, 0.06) is True
    assert passes_edge(0.65, 0.04) is False
    assert passes_edge(0.50, 0.09) is True
    assert passes_edge(0.30, 0.50) is False  # below floor
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement**
```python
# utils/edge_filter.py
def passes_edge(confidence: float, edge: float) -> bool:
    if confidence < 0.35:
        return False
    if confidence >= 0.80:
        return edge >= 0.03
    if confidence >= 0.60:
        return edge >= 0.05
    return edge >= 0.08
```
- [ ] **Step 4: Run → PASS. Step 5: Commit** `git commit -m "feat(directional): tiered edge filter (port)"`

---

### Task 4: Directional models

**Files:**
- Create: `core/directional/__init__.py` (empty), `core/directional/models.py`
- Test: `tests/directional/test_models.py`

**Interfaces:**
- Produces:
  - `DirectionalCandidate(market_id: str, title: str, category: str, side: str, market_price: float, ai_probability: float|None, confidence: float|None, edge: float, strategy: str, reasoning: str = "")`
  - `DirectionalOrder(market_id, side, price: float, size: int, notional: float, strategy: str, reasoning: str = "")`
  - `DirectionalPosition(market_id, side, entry_price, size, strategy, mode, opened_at, stop_loss: float|None, take_profit: float|None, status: str = "open")`
  - All `@dataclass`. `DirectionalOrder` exposes `.market_id`, `.notional`, `.side` for the risk Protocol.

- [ ] **Step 1: Failing test**
```python
from core.directional.models import DirectionalCandidate, DirectionalOrder, DirectionalPosition
def test_order_has_risk_protocol_fields():
    o = DirectionalOrder(market_id="kalshi:KX-1", side="NO", price=0.9, size=5, notional=4.5, strategy="safe_compounder")
    assert (o.market_id, o.notional, o.side) == ("kalshi:KX-1", 4.5, "NO")
def test_candidate_and_position_construct():
    c = DirectionalCandidate(market_id="kalshi:KX-1", title="t", category="Sports", side="NO",
                             market_price=0.9, ai_probability=None, confidence=None, edge=0.04, strategy="safe_compounder")
    assert c.edge == 0.04
    p = DirectionalPosition(market_id="kalshi:KX-1", side="NO", entry_price=0.9, size=5,
                            strategy="safe_compounder", mode="paper", opened_at="2026-06-18T00:00:00",
                            stop_loss=None, take_profit=None)
    assert p.status == "open"
```
- [ ] **Step 2: FAIL → Step 3: implement the three dataclasses → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): core models"`

---

### Task 5: Config — DirectionalConfig + config.yaml block

**Files:**
- Modify: `utils/config_loader.py` (add DirectionalConfig dataclasses + wire into the top-level config build, mirroring how IntelligenceConfig is built)
- Modify: `config.yaml` (append the `directional:` block from the spec)
- Test: `tests/directional/test_config.py`

**Interfaces:**
- Produces: `config.directional` with `.enabled: bool`, `.scan_interval_seconds: int`, `.markets_per_cycle: int`, `.category_exclude: list`, `.caps.total_exposure/max_position/max_open`, `.safe_compounder.mode/min_edge_cents/skip_categories`, `.ai_directional.mode/min_confidence/min_edge_pct/kelly_fraction/stop_loss_pct/take_profit_pct/max_hold_hours`. Missing block → all defaults, `enabled=False`.

- [ ] **Step 1: Failing test**
```python
from utils.config_loader import load_config
def test_directional_defaults_when_absent(tmp_path):
    cfg = load_config("config.yaml")  # existing config without changes still parses
    assert hasattr(cfg, "directional")
    assert cfg.directional.enabled is False
    assert cfg.directional.caps.total_exposure == 30
```
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement** DirectionalCaps/SafeCompounderCfg/AiDirectionalCfg/DirectionalConfig dataclasses with the defaults above; add `directional=_build_directional(raw.get("directional", {}) or {})` to the config build (copy the `_build_intelligence_config` nesting pattern). Append `directional:` block to `config.yaml` (enabled:false).
- [ ] **Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): config schema + defaults"`

---

### Task 6: Risk Protocol + directional caps + exposure tracking

**Files:**
- Modify: `core/risk_manager.py` (add `Order` Protocol; add `directional_max_total_exposure/position/open` to RiskConfig; add `check_directional_order(order) -> bool` + separate directional exposure register/release)
- Test: `tests/directional/test_risk_directional.py`

**Interfaces:**
- Consumes: `DirectionalOrder` (has `.market_id`, `.notional`, `.side`).
- Produces: `RiskManager.check_directional_order(order, open_count: int, directional_exposure: float) -> bool` — rejects if kill switch on, notional > directional_max_position, directional_exposure+notional > directional_max_total_exposure, or open_count >= directional_max_open.

- [ ] **Step 1: Failing test**
```python
from core.risk_manager import RiskManager, RiskConfig
from core.directional.models import DirectionalOrder
def _o(notional): return DirectionalOrder("kalshi:KX-1","NO",0.9,5,notional,"safe_compounder")
def test_directional_caps():
    rm = RiskManager(RiskConfig(directional_max_total_exposure=30, directional_max_position=8, directional_max_open=4))
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=0) is True
    assert rm.check_directional_order(_o(9), open_count=0, directional_exposure=0) is False   # > per-position
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=25) is False  # > total
    assert rm.check_directional_order(_o(8), open_count=4, directional_exposure=0) is False   # too many open
def test_directional_respects_kill_switch():
    rm = RiskManager(RiskConfig()); rm._trigger_kill_switch("test")
    assert rm.check_directional_order(_o(1), 0, 0) is False
```
- [ ] **Step 2: FAIL → Step 3: implement (add fields w/ defaults 30/8/4; method checks in order: kill switch, per-position, total, open count) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): risk caps + Order protocol"`

---

### Task 7: SQLite store (positions + signals)

**Files:**
- Create: `core/directional/store.py`
- Test: `tests/directional/test_store.py`

**Interfaces:**
- Produces: `DirectionalStore(db_path)` with `init_schema()`, `record_signal(candidate, placed: bool) -> int`, `record_position(position) -> int`, `update_position(market_id, **fields)`, `open_positions() -> list[DirectionalPosition]`, `recent_signals(limit=50) -> list[dict]`, `directional_exposure() -> float` (sum of open notionals), `pnl_summary() -> dict`.

- [ ] **Step 1: Failing test**
```python
from core.directional.store import DirectionalStore
from core.directional.models import DirectionalPosition
def test_roundtrip(tmp_path):
    s = DirectionalStore(str(tmp_path/"d.db")); s.init_schema()
    pid = s.record_position(DirectionalPosition("kalshi:KX-1","NO",0.9,5,"safe_compounder","paper","2026-06-18T00:00:00",None,None))
    assert pid > 0
    assert len(s.open_positions()) == 1
    assert s.directional_exposure() == 0.9*5
    s.update_position("kalshi:KX-1", status="closed")
    assert len(s.open_positions()) == 0
```
- [ ] **Step 2: FAIL → Step 3: implement two tables (directional_positions, directional_signals) + the CRUD above (sqlite3, mirror utils/signal_db.py style) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): SQLite store"`

---

### Task 8: Market scanner (events + nested, parlay filter)

**Files:**
- Create: `core/directional/scanner.py`
- Test: `tests/directional/test_scanner.py`

**Interfaces:**
- Consumes: `kalshi_client` (needs an events-with-nested fetch). If `kalshi_client/api.py` lacks it, add `async def list_open_markets(self, max_markets: int) -> list[KalshiMarket]` that paginates `/events?with_nested_markets=true&status=open` (adapt `/tmp/kalshi-ai-bot/src/jobs/ingest.py`); `categorize` (Task 1).
- Produces: `KalshiMarketScanner(kalshi_client, categorize_fn, min_volume, exclude_categories)` with `async scan(max_markets) -> list[KalshiMarket]` (parlay-filtered, volume-floored, category-tagged via `market.category`).
- Helper: `is_tradeable(market) -> bool` — False when both YES-ask and NO-ask ≈ $1 (collection/parlay).

- [ ] **Step 1: Failing test (mock client)**
```python
import pytest
from core.directional.scanner import KalshiMarketScanner, is_tradeable
from kalshi_client.models import KalshiMarket
def mk(t, yes=0.4, vol=1000): return KalshiMarket(ticker=t, event_ticker=t.split("-")[0], series_ticker=t.split("-")[0], title=t, yes_price=yes, volume=vol)
def test_is_tradeable_rejects_parlay():
    assert is_tradeable(mk("KX-1", yes=0.4)) is True
    # parlay proxy: near-1 yes AND near-1 no is impossible for a real binary; model it via a flag/price check used in impl
@pytest.mark.asyncio
async def test_scan_filters_low_volume(monkeypatch):
    class C:
        async def list_open_markets(self, max_markets): return [mk("KXNFLGAME-1", vol=5), mk("KXNFLGAME-2", vol=5000)]
    sc = KalshiMarketScanner(C(), categorize_fn=lambda t: "Sports", min_volume=100, exclude_categories=[])
    out = await sc.scan(max_markets=50)
    assert [m.ticker for m in out] == ["KXNFLGAME-2"]
```
- [ ] **Step 2: FAIL → Step 3: implement `list_open_markets` (if needed) + scanner (await client, filter is_tradeable + volume + category not excluded, set m.category) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): Kalshi scanner + parlay filter"`

---

### Task 9: Strategy base + Safe Compounder

**Files:**
- Create: `core/directional/strategies/__init__.py`, `core/directional/strategies/base.py`, `core/directional/strategies/safe_compounder.py` (math adapted from `/tmp/kalshi-ai-bot/src/strategies/safe_compounder.py`)
- Test: `tests/directional/test_safe_compounder.py`

**Interfaces:**
- Produces: `Strategy` ABC with `name: str` and `async scan(markets, ctx) -> list[DirectionalCandidate]`. `SafeCompounder(min_edge_cents, skip_categories)`: for each market estimate true NO prob from YES last price + time-to-expiry; if cheapest NO ask gives edge ≥ `min_edge_cents`, emit a NO candidate at resting price (ask − 0.01). Skip excluded categories.

- [ ] **Step 1: Failing test**
```python
import pytest
from core.directional.strategies.safe_compounder import SafeCompounder
from kalshi_client.models import KalshiMarket
@pytest.mark.asyncio
async def test_emits_no_candidate_on_cheap_no(monkeypatch):
    # YES trading at 0.04 (near-certain NO); NO ask 0.93 => edge vs ~0.96 fair = 3c
    m = KalshiMarket(ticker="KXMLB-1", event_ticker="KXMLB", series_ticker="KXMLB", title="x", yes_price=0.04, volume=5000, category="Sports")
    sc = SafeCompounder(min_edge_cents=3, skip_categories=["entertainment"])
    # ctx provides a function to fetch the NO ask for a market
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert len(cands) == 1 and cands[0].side == "NO"
    assert cands[0].market_price <= 0.93
@pytest.mark.asyncio
async def test_skips_when_edge_too_small():
    m = KalshiMarket(ticker="KXMLB-1", event_ticker="KXMLB", series_ticker="KXMLB", title="x", yes_price=0.04, category="Sports", volume=5000)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.97})  # ~0c edge
    assert cands == []
```
- [ ] **Step 2: FAIL → Step 3: implement base ABC + SafeCompounder (fair_no ≈ 1 − yes_price; edge_cents = (fair_no − no_ask)*100; emit if ≥ min; price = no_ask − 0.01; skip categories) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): Safe Compounder strategy"`

---

### Task 10: AI-directional strategy

**Files:**
- Create: `core/directional/strategies/ai_directional.py`
- Test: `tests/directional/test_ai_directional.py`

**Interfaces:**
- Consumes: `intelligence_engine.evaluate(market_id, market_question, current_yes_price, arb_edge, resolution_criteria=None) -> SignalSummary` (fields `.signal.ai_probability/.confidence/.direction/.edge_vs_market/.reasoning`); `passes_edge` (Task 3); `structural_score` (Task 2).
- Produces: `AiDirectional(intelligence_engine, min_confidence, min_edge_pct)`: for each market call evaluate; if `signal.confidence ≥ min_confidence` and `passes_edge(conf, |edge_vs_market|)` and `|edge_vs_market| ≥ min_edge_pct`, emit a candidate (side = "YES" if direction=="bullish" else "NO"), folding `structural_score` into `.edge`. Fail-safe: any exception → skip market.

- [ ] **Step 1: Failing test (mock intelligence)**
```python
import pytest
from types import SimpleNamespace
from core.directional.strategies.ai_directional import AiDirectional
from kalshi_client.models import KalshiMarket
class FakeIntel:
    async def evaluate(self, **k):
        sig = SimpleNamespace(ai_probability=0.7, confidence=0.85, direction="bullish", edge_vs_market=0.12, reasoning="news")
        return SimpleNamespace(signal=sig)
@pytest.mark.asyncio
async def test_emits_yes_on_strong_bullish():
    m = KalshiMarket(ticker="KXCPI-1", event_ticker="KXCPI", series_ticker="KXCPI", title="x", yes_price=0.58, category="Finance", volume=9000)
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    assert len(cands) == 1 and cands[0].side == "YES" and cands[0].confidence == 0.85
@pytest.mark.asyncio
async def test_skips_low_confidence():
    class Weak(FakeIntel):
        async def evaluate(self, **k):
            return SimpleNamespace(signal=SimpleNamespace(ai_probability=0.5, confidence=0.4, direction="bullish", edge_vs_market=0.2, reasoning=""))
    s = AiDirectional(Weak(), 0.60, 0.05)
    assert await s.scan([m()], ctx={}) == [] if False else True  # see impl note
```
(Fix the second test to construct a market like the first; assert `== []`.)
- [ ] **Step 2: FAIL → Step 3: implement (try/except per market; gates: confidence, passes_edge, min_edge_pct; side from direction; edge = |edge_vs_market| + structural_score) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): AI-directional strategy (reuses intelligence)"`

---

### Task 11: Decider (Kelly + risk gate)

**Files:**
- Create: `core/directional/decider.py`
- Test: `tests/directional/test_decider.py`

**Interfaces:**
- Consumes: `kelly_fraction(...)` (core/kelly.py); `RiskManager.check_directional_order(...)` (Task 6); `DirectionalStore.directional_exposure()/open_positions()` (Task 7).
- Produces: `Decider(risk_manager, store, kelly_frac, max_position_usd, cash_balance_fn)` with `decide(candidate) -> DirectionalOrder|None`. Sizing: AI uses `kelly_fraction(edge, yes_price, ai_probability, confidence, fraction=kelly_frac)` × cash; Safe Compounder uses fixed `max_position_usd` (no AI prob). Cap notional ≤ max_position_usd, size = floor(notional/price), notional = size*price. Return None if size<1 or risk check fails.

- [ ] **Step 1: Failing test**
```python
from core.directional.decider import Decider
from core.directional.models import DirectionalCandidate
class RM:
    def check_directional_order(self, o, open_count, directional_exposure): return o.notional <= 8
class ST:
    def directional_exposure(self): return 0.0
    def open_positions(self): return []
def cand(strategy, conf=0.85): return DirectionalCandidate("kalshi:KX-1","NO",0.9,0.0 if strategy=="safe_compounder" else 0.7, conf if strategy!="safe_compounder" else None, 0.05,"x", strategy="safe_compounder" if strategy=="safe_compounder" else "ai_directional")
def test_safe_compounder_fixed_size():
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda:30)
    o = d.decide(cand("safe_compounder"))
    assert o is not None and o.notional <= 8 and o.size >= 1
def test_rejected_when_over_cap():
    d = Decider(RM(), ST(), 0.25, max_position_usd=20, cash_balance_fn=lambda:100)
    # 20 > risk cap of 8 -> None
    assert d.decide(cand("safe_compounder")) is None
```
(Align the `DirectionalCandidate(...)` positional args with Task 4's signature when implementing.)
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): decider (Kelly + risk gate)"`

---

### Task 12: Executor (paper / live)

**Files:**
- Create: `core/directional/executor.py`
- Test: `tests/directional/test_executor.py`

**Interfaces:**
- Consumes: `kalshi_client.place_order(ticker=..., token_type=..., side=..., price=..., size=..., strategy_tag=...)`; `DirectionalStore.record_position`.
- Produces: `Executor(kalshi_client, store)` with `async place(order, mode, stop_loss=None, take_profit=None) -> DirectionalPosition|None`. paper: record position (mode="paper") at order.price, NO API call. live: call place_order, on success record position (mode="live").

- [ ] **Step 1: Failing test**
```python
import pytest
from core.directional.executor import Executor
from core.directional.models import DirectionalOrder
class Store:
    def __init__(self): self.saved=[]
    def record_position(self, p): self.saved.append(p); return 1
class Client:
    def __init__(self): self.calls=0
    async def place_order(self, **k): self.calls+=1; return object()
@pytest.mark.asyncio
async def test_paper_records_no_api():
    st, cl = Store(), Client()
    pos = await Executor(cl, st).place(DirectionalOrder("kalshi:KX-1","NO",0.9,5,4.5,"safe_compounder"), mode="paper")
    assert cl.calls == 0 and len(st.saved) == 1 and st.saved[0].mode == "paper"
@pytest.mark.asyncio
async def test_live_calls_place_order():
    st, cl = Store(), Client()
    pos = await Executor(cl, st).place(DirectionalOrder("kalshi:KX-1","NO",0.9,5,4.5,"safe_compounder"), mode="live")
    assert cl.calls == 1 and st.saved[0].mode == "live"
```
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): executor (paper/live)"`

---

### Task 13: Tracker (stop-loss/TP/time + resolution sweep)

**Files:**
- Create: `core/directional/tracker.py` (StopLossCalculator adapted from `/tmp/kalshi-ai-bot/src/utils/stop_loss_calculator.py`)
- Test: `tests/directional/test_tracker.py`

**Interfaces:**
- Consumes: `DirectionalStore` (open_positions/update_position), `kalshi_client.get_market`/`get_orderbook_unified` (current price + resolution), `Executor` (to place closing orders in live).
- Produces: `Tracker(store, kalshi_client, executor)` with `async sweep(now)`:
  - For AI positions: compute current price; if `should_exit(position, current_price, now)` (stop-loss hit / take-profit hit / age > max_hold_hours) → close (paper: mark closed at current; live: place opposing order) and book P&L.
  - For all: if market resolved → settle at 1.0/0.0, mark closed, record realized P&L.
  - `should_exit(position, price, now) -> tuple[bool, str]` pure function (test this directly).

- [ ] **Step 1: Failing test (pure exit logic)**
```python
from core.directional.tracker import should_exit
from core.directional.models import DirectionalPosition
def pos(side="YES", entry=0.6, sl=0.42, tp=0.9): 
    return DirectionalPosition("kalshi:KX-1", side, entry, 5, "ai_directional", "paper", "2026-06-18T00:00:00", sl, tp)
def test_stop_loss_triggers():
    ok, why = should_exit(pos(), price=0.40, now="2026-06-18T01:00:00", max_hold_hours=72)
    assert ok and why == "stop_loss"
def test_take_profit_triggers():
    ok, why = should_exit(pos(), price=0.92, now="2026-06-18T01:00:00", max_hold_hours=72)
    assert ok and why == "take_profit"
def test_time_exit():
    ok, why = should_exit(pos(), price=0.6, now="2026-06-22T00:00:00", max_hold_hours=72)
    assert ok and why == "max_hold"
def test_hold_otherwise():
    ok, _ = should_exit(pos(), price=0.6, now="2026-06-18T02:00:00", max_hold_hours=72)
    assert ok is False
```
- [ ] **Step 2: FAIL → Step 3: implement `should_exit` (YES: stop if price≤sl, tp if price≥tp; NO: invert; age from opened_at vs now) + `sweep()` wiring → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): tracker exits + resolution sweep"`

---

### Task 14: DirectionalEngine (loop wiring)

**Files:**
- Create: `core/directional/engine.py`
- Test: `tests/directional/test_engine.py`

**Interfaces:**
- Consumes: scanner, strategies list, decider, executor, tracker, store, config.
- Produces: `DirectionalEngine(config, kalshi_client, intelligence_engine, risk_manager)` with `async run_once()` (one scan→strategies→decide→execute→sweep pass; per-strategy mode from config) and `async run_forever()` (loop on scan_interval, whole body try/except, logs+continues). Factory wires the components.

- [ ] **Step 1: Failing test (one pass, paper, fixtures)**
```python
import pytest
@pytest.mark.asyncio
async def test_run_once_paper_records_intended(monkeypatch):
    # Build engine with a fake scanner returning 1 cheap-NO market, SafeCompounder enabled in paper.
    # Assert: store has 1 paper position, kalshi_client.place_order NOT called.
    ...
```
(Construct with fakes mirroring earlier task fakes; assert paper path records without live calls.)
- [ ] **Step 2: FAIL → Step 3: implement engine.run_once/run_forever + factory → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): engine loop"`

---

### Task 15: Launch wiring (gated, isolated)

**Files:**
- Modify: `run_with_dashboard.py` (after the existing loops are set up; ~8 lines)
- Test: manual smoke (no unit test for asyncio task creation) — documented check.

**Interfaces:**
- Consumes: `config.directional.enabled`, `DirectionalEngine`.

- [ ] **Step 1: Add the gated launch**
```python
# in run_with_dashboard start(), AFTER kalshi_client + intelligence_engine exist:
if getattr(self.config, "directional", None) and self.config.directional.enabled:
    from core.directional.engine import DirectionalEngine
    self.directional_engine = DirectionalEngine(
        self.config.directional, self.kalshi_client, self.intelligence_engine, self.risk_manager)
    asyncio.create_task(self._guarded(self.directional_engine.run_forever(), "directional"))
# helper:
async def _guarded(self, coro, name):
    try: await coro
    except Exception as e: logger.error(f"[{name}] loop crashed (isolated): {e}", exc_info=True)
```
- [ ] **Step 2: Verify the live bot still boots with `directional.enabled: false`** (no behavior change):
Run: rebuild+recreate in a scratch check, confirm logs show no directional start and arb unaffected. (Do NOT enable.)
- [ ] **Step 3: Commit** `git commit -m "feat(directional): gated, isolated launch in run_with_dashboard"`

---

### Task 16: Dashboard — /api/directional + panel

**Files:**
- Modify: `dashboard/server.py` (add `GET /api/directional` reading the DirectionalStore; add a "Directional" HTML section + JS poll mirroring existing panels)
- Test: `tests/directional/test_api_directional.py`

**Interfaces:**
- Consumes: `DirectionalStore.open_positions()/recent_signals()/pnl_summary()`.
- Produces: `GET /api/directional` → `{"strategies": [...], "positions": [...], "signals": [...], "pnl": {...}}`.

- [ ] **Step 1: Failing test (seed store, call handler)**
```python
def test_api_shape(tmp_path):
    from core.directional.store import DirectionalStore
    s = DirectionalStore(str(tmp_path/"d.db")); s.init_schema()
    # seed one position + one signal, then call the pure builder used by the endpoint:
    from dashboard.server import build_directional_payload
    payload = build_directional_payload(s)
    assert set(payload) >= {"strategies","positions","signals","pnl"}
```
- [ ] **Step 2: FAIL → Step 3: implement `build_directional_payload(store)` + wire the FastAPI route + add the HTML section (status cards, positions table, decision feed, P&L) following the existing panel/JS pattern → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): dashboard panel + /api/directional"`

---

### Task 17: Wire store + scanner context into the engine end-to-end (integration)

**Files:**
- Modify: `core/directional/engine.py` (ensure store path from config, scanner ctx provides `no_ask`/current-price closures over kalshi_client)
- Test: `tests/directional/test_integration_paper.py`

**Interfaces:**
- Produces: a full paper run on fixture markets that records intended Safe Compounder + AI positions and books a resolution P&L on a resolved fixture.

- [ ] **Step 1: Failing integration test** (fake kalshi_client serving 2 markets + orderbooks + one resolved; both strategies paper-enabled; assert store has 2 open positions, then `tracker.sweep` settles the resolved one with correct P&L sign).
- [ ] **Step 2: FAIL → Step 3: finish wiring → Step 4: PASS → Step 5: Commit** `git commit -m "test(directional): end-to-end paper integration"`

---

## Self-Review
- **Spec coverage:** scanner (T8), Safe Compounder (T9), AI-directional (T10, reuses intelligence), decider/Kelly (T11), executor paper/live (T12), tracker hybrid exits (T13), separate caps (T6), persistence (T7), config (T5), launch isolation (T15), dashboard (T16), structural bias + categories (T1/T2), edge filter (T3), models (T4), integration (T17). All spec sections mapped.
- **Placeholder scan:** test bodies for T14/T16/T17 describe fakes rather than full code — implementers must mirror the fakes from earlier tasks (noted inline). Acceptable as they compose already-shown fixtures; everything novel has real code.
- **Type consistency:** `DirectionalOrder(.market_id/.notional/.side)` used by risk Protocol (T6) matches T4; `check_directional_order(order, open_count, directional_exposure)` consistent T6/T11; `evaluate(...) -> SignalSummary.signal.{ai_probability,confidence,direction,edge_vs_market}` matches the real engine; `kelly_fraction(edge, yes_price, ai_probability, confidence, fraction)` matches core/kelly.py.

## Notes for implementer
- Keep each new file focused; no edits to `core/arb_engine.py`, `core/execution.py`, the cross-platform monitor, or DataFeed.
- Run the suite after each task: `docker exec -w /app polymarket-arb python -m pytest tests/directional -q` (after `docker cp` of new files) OR a local venv with requirements.
- Do NOT set any `mode: live` or `directional.enabled: true` during implementation — that's a separate, deliberate go-live step after paper validation.
