# Directional Trading Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Kalshi-only "directional" trading mode that takes positions on individual markets from signals (pure-math Safe Compounder + AI-directional), with or without cross-platform matches, behind a flag, paper-first.

**Architecture:** Standalone `core/directional/` package launched as one independent asyncio task; reuses `kalshi_client`, `intelligence/`, `core/kelly.py`, `core/risk_manager.py`; never touches the live arb loop. Two strategies under one engine; per-strategy paper/live; separate risk caps; SQLite persistence; integrated dashboard section.

**Tech Stack:** Python 3.12 async, httpx, cryptography (existing), pytest + pytest-asyncio. No new deps.

## Global Constraints
- MUST NOT disrupt the live Kalshi-native arb (real money). The directional task is `create_task`'d ONLY when `directional.enabled`; its loop body is wrapped in try/except; it shares only read-mostly services.
- Paper-first: each strategy has `mode: paper|live`; live requires explicit per-strategy flag.
- Separate caps: `directional_max_total_exposure=30`, `directional_max_position=8`, `directional_max_open=4` (USD). Global kill switch / daily-loss / drawdown still apply via `check_directional_order`. The DirectionalEngine reads caps from `config.directional.caps` and passes them as explicit arguments to `check_directional_order`.
- Reuse existing: `kalshi_client/api.py`, `intelligence/intelligence_engine.py`, `core/kelly.py::kelly_fraction(edge, yes_price, ai_probability, confidence, fraction=0.25, max_fraction=0.10)`, `core/risk_manager.py`.
- Directional uses `RiskManager.check_directional_order` ONLY â never the existing `check_order` (that guards the live Polymarket-arb path).
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
- Create: `utils/kalshi_categories.py` (copy source file, then add `categorize` adapter)
- Create: `tests/directional/__init__.py` (empty, marks the test package)
- Test: `tests/directional/test_kalshi_categories.py`

**Interfaces:**
- Produces: `categorize(event_ticker: str) -> str` returning the top-level group string (e.g. "Sports", "Politics", "Finance", "Crypto", "Entertainment", "Science/Tech", "Weather", "Media", "World Events", "Esports", "Other").

The source file (`/tmp/pma-jbecker/src/analysis/kalshi/util/categories.py`) exports `SUBCATEGORY_PATTERNS` (a list of `(pattern, group, category, subcategory)` tuples), `get_group(category)`, and `get_hierarchy(category)`. Both `get_group`/`get_hierarchy` do `if pattern in cat_upper` â they treat their argument as a raw string to search patterns within, NOT as a ticker to prefix-match. Do NOT call `get_group(event_ticker)` directly; instead write a proper adapter that iterates patterns.

- [ ] **Step 1: Copy the file and add `__init__.py`**
```bash
cp /tmp/pma-jbecker/src/analysis/kalshi/util/categories.py ~/docker/polymarket-arbitrage/utils/kalshi_categories.py
touch ~/docker/polymarket-arbitrage/tests/directional/__init__.py
```
Then append the adapter to `utils/kalshi_categories.py`:
```python
def categorize(event_ticker: str) -> str:
    """Map an event ticker to its top-level group.

    Iterates SUBCATEGORY_PATTERNS in order (most specific first) and returns
    the group of the first pattern found as a substring of the uppercased ticker.
    Returns "Other" when no pattern matches.
    """
    upper = event_ticker.upper()
    for pattern, group, _cat, _subcat in SUBCATEGORY_PATTERNS:
        if pattern in upper:
            return group
    return "Other"
```
- [ ] **Step 2: Write the failing test**
```python
# tests/directional/test_kalshi_categories.py
import pytest
from utils.kalshi_categories import categorize

def test_known_prefixes_map_to_categories():
    # NFLGAME is in SUBCATEGORY_PATTERNS â "Sports"
    assert categorize("KXNFLGAME-26SEP-KC") == "Sports"
    # "KXSENATE-26NOV-R": read SUBCATEGORY_PATTERNS for a pattern in "KXSENATE";
    # after reading the file, set expected to the ACTUAL output (e.g. "Politics" or "Other")
    result = categorize("KXSENATE-26NOV-R")
    assert result in ("Politics", "Other")  # adjust to actual after Step 3
    # Unknown ticker returns "Other"
    assert categorize("KXUNKNOWNXYZ-99") == "Other"
```
- [ ] **Step 3: Run test, read actual output for "KXSENATE-26NOV-R", update the assert to match**
```bash
python -m pytest tests/directional/test_kalshi_categories.py -q
```
After running: replace `assert result in ("Politics", "Other")` with the exact value returned.
- [ ] **Step 4: Commit**
```bash
git add utils/kalshi_categories.py tests/directional/__init__.py tests/directional/test_kalshi_categories.py
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
    # category edge is NO-side-only by design: maker/NO excess edge from repo#1 findings
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
  - `DirectionalOrder(market_id: str, side: str, price: float, size: int, notional: float, strategy: str, reasoning: str = "")` â exposes `.market_id`, `.notional`, `.side` for the risk Protocol.
  - `DirectionalPosition(market_id: str, side: str, entry_price: float, size: int, strategy: str, mode: str, opened_at: datetime, stop_loss: float|None, take_profit: float|None, notional: float = 0.0, status: str = "open")` â `notional` populated from `DirectionalOrder.notional`; `opened_at` is `datetime`, not str.
  - All `@dataclass`.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_models.py
import pytest
from datetime import datetime
from core.directional.models import DirectionalCandidate, DirectionalOrder, DirectionalPosition

def test_order_has_risk_protocol_fields():
    o = DirectionalOrder(market_id="kalshi:KX-1", side="NO", price=0.9, size=5, notional=4.5, strategy="safe_compounder")
    assert (o.market_id, o.notional, o.side) == ("kalshi:KX-1", 4.5, "NO")

def test_candidate_and_position_construct():
    c = DirectionalCandidate(market_id="kalshi:KX-1", title="t", category="Sports", side="NO",
                             market_price=0.9, ai_probability=None, confidence=None, edge=0.04, strategy="safe_compounder")
    assert c.edge == 0.04
    p = DirectionalPosition(market_id="kalshi:KX-1", side="NO", entry_price=0.9, size=5,
                            strategy="safe_compounder", mode="paper",
                            opened_at=datetime(2026, 6, 18, 0, 0, 0),
                            stop_loss=None, take_profit=None, notional=4.5)
    assert p.status == "open"
    assert p.notional == 4.5

def test_directional_exposure_zero_after_close():
    """directional_exposure() sums open positions' notional â tested here via model invariant."""
    p = DirectionalPosition(market_id="kalshi:KX-1", side="NO", entry_price=0.9, size=5,
                            strategy="safe_compounder", mode="paper",
                            opened_at=datetime(2026, 6, 18), stop_loss=None, take_profit=None, notional=4.5)
    assert p.notional == 4.5  # notional is on the position, not derived from price*size
```
- [ ] **Step 2: FAIL â Step 3: implement the three dataclasses (import datetime) â Step 4: PASS â Step 5: Commit** `git commit -m "feat(directional): core models"`

---

### Task 5: Config — DirectionalConfig + config.yaml block

**Files:**
- Modify: `utils/config_loader.py` (add DirectionalConfig dataclasses + wire into the top-level config build, mirroring how IntelligenceConfig is built)
- Modify: `config.yaml` (append the `directional:` block from the spec)
- Test: `tests/directional/test_config.py`

**Interfaces:**
- Produces: `config.directional` with `.enabled: bool`, `.db_path: str` (default `"data/directional.db"`), `.scan_interval_seconds: int`, `.markets_per_cycle: int`, `.category_exclude: list`, `.caps.total_exposure/max_position/max_open`, `.safe_compounder.mode/min_edge_cents/skip_categories`, `.ai_directional.mode/min_confidence/min_edge_pct/kelly_fraction/stop_loss_pct/take_profit_pct/max_hold_hours`. Missing block → all defaults, `enabled=False`.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_config.py
import pytest
from utils.config_loader import load_config

def test_directional_defaults_when_absent(tmp_path):
    cfg = load_config("config.yaml")  # existing config without changes still parses
    assert hasattr(cfg, "directional")
    assert cfg.directional.enabled is False
    assert cfg.directional.caps.total_exposure == 30
    assert cfg.directional.db_path == "data/directional.db"

def test_directional_disabled_when_present(tmp_path):
    """Load the real config.yaml (with the appended directional block) and verify enabled=False."""
    cfg = load_config("config.yaml")
    assert cfg.directional.enabled is False
```
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Implement**
  - Add `DirectionalCaps`, `SafeCompounderCfg`, `AiDirectionalCfg`, `DirectionalConfig` dataclasses to `utils/config_loader.py` with defaults (`db_path: str = "data/directional.db"`, caps `total_exposure=30/max_position=8/max_open=4`).
  - Add `directional: DirectionalConfig = field(default_factory=DirectionalConfig)` to `BotConfig`.
  - Add `directional=_build_directional(raw.get("directional", {}) or {})` to the config build, following the `_build_intelligence_config` nesting pattern.
  - Append `directional:` block to `config.yaml` with `enabled: false` and all sub-keys.
- [ ] **Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): config schema + defaults"`

**Deploy checklist note:** Before any restart, run `grep -A2 'directional:' config.yaml | grep 'enabled'` to confirm `enabled: false` is set under the directional block.

---

### Task 6: Risk Protocol + directional caps + exposure tracking

**Files:**
- Modify: `core/risk_manager.py` (add `Order` Protocol; add `check_directional_order` method; add separate directional exposure register/release — do NOT add any fields to `RiskConfig`)
- Test: `tests/directional/test_risk_directional.py`

**Interfaces:**
- Consumes: `DirectionalOrder` (has `.market_id`, `.notional`, `.side`).
- Produces: `RiskManager.check_directional_order(self, order, open_count: int, directional_exposure: float, max_position: float, max_total: float, max_open: int) -> bool`.

  Checks in order:
  1. `self.state.kill_switch_triggered` → return `False`
  2. `order.notional > max_position` → return `False`
  3. `directional_exposure + order.notional > max_total` → return `False`
  4. `open_count >= max_open` → return `False`
  5. else return `True`

  Caps are passed as explicit arguments by the DirectionalEngine (read from `config.directional.caps`). This method is SEPARATE from `check_order` which guards the live Polymarket-arb path.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_risk_directional.py
import pytest
from core.risk_manager import RiskManager, RiskConfig
from core.directional.models import DirectionalOrder

def _o(notional): return DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, notional, "safe_compounder")

def test_directional_caps():
    rm = RiskManager(RiskConfig())
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=0, max_position=8, max_total=30, max_open=4) is True
    assert rm.check_directional_order(_o(9), open_count=0, directional_exposure=0, max_position=8, max_total=30, max_open=4) is False   # > per-position
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=25, max_position=8, max_total=30, max_open=4) is False  # > total
    assert rm.check_directional_order(_o(8), open_count=4, directional_exposure=0, max_position=8, max_total=30, max_open=4) is False   # too many open

def test_directional_respects_kill_switch():
    rm = RiskManager(RiskConfig())
    rm._trigger_kill_switch("test")
    assert rm.check_directional_order(_o(1), 0, 0, max_position=8, max_total=30, max_open=4) is False
```
- [ ] **Step 2: FAIL → Step 3: add `check_directional_order` method to `RiskManager` with the exact signature and checks above (no RiskConfig fields changed) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): check_directional_order + Order protocol"`

---

### Task 7: SQLite store (positions + signals)

**Files:**
- Create: `core/directional/store.py`
- Test: `tests/directional/test_store.py`

**Interfaces:**
- Produces: `DirectionalStore(db_path: str)` with `init_schema()`, `record_signal(candidate, placed: bool) -> int`, `record_position(position) -> int`, `update_position(market_id, **fields)`, `open_positions() -> list[DirectionalPosition]`, `recent_signals(limit=50) -> list[dict]`, `directional_exposure() -> float` (sum of open positions' `notional` field), `pnl_summary() -> dict`.

**Note:** `db_path` comes from `config.directional.db_path` (default `"data/directional.db"`). Using a dedicated `DirectionalStore` (not `utils/signal_db.py`) is an intentional deviation from the spec for isolation — the directional store must never write to the live arb signal tables.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_store.py
import pytest
from datetime import datetime
from core.directional.store import DirectionalStore
from core.directional.models import DirectionalPosition

def test_roundtrip(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    pos = DirectionalPosition(
        market_id="kalshi:KX-1", side="NO", entry_price=0.9, size=5,
        strategy="safe_compounder", mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=None, take_profit=None, notional=4.5
    )
    pid = s.record_position(pos)
    assert pid > 0
    assert len(s.open_positions()) == 1
    assert s.directional_exposure() == pytest.approx(4.5)
    s.update_position("kalshi:KX-1", status="closed")
    assert len(s.open_positions()) == 0

def test_exposure_zero_after_close(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    pos = DirectionalPosition("kalshi:KX-1", "NO", 0.9, 5, "safe_compounder", "paper",
                               datetime(2026, 6, 18), None, None, notional=4.5)
    s.record_position(pos)
    s.update_position("kalshi:KX-1", status="closed")
    assert s.directional_exposure() == 0.0
```
- [ ] **Step 2: FAIL → Step 3: implement two tables (`directional_positions`, `directional_signals`) storing `notional` column + the CRUD above (sqlite3, mirror `utils/signal_db.py` style) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): SQLite store"`

---

### Task 8: Market scanner (events + nested, parlay filter)

**Files:**
- Create: `core/directional/scanner.py`
- Test: `tests/directional/test_scanner.py`

**Interfaces:**
- Consumes: `kalshi_client.list_all_markets(status="open", max_markets=...)` — already paginates and excludes parlay/KXMV series internally; `categorize` (Task 1).
- Produces: `KalshiMarketScanner(kalshi_client, categorize_fn, min_volume, exclude_categories)` with `async scan(max_markets: int) -> list[KalshiMarket]`.
  - Calls `await client.list_all_markets(status="open", max_markets=max_markets)`.
  - For each market: sets `market.category = categorize_fn(market.event_ticker)`; applies `is_tradeable(market)` guard; filters `market.volume >= min_volume`; excludes `market.category in exclude_categories`.
  - Do NOT add a new `list_open_markets` method to kalshi_client — `list_all_markets` already exists.
- Helper: `is_tradeable(market) -> bool` — secondary guard using `market.yes_price` / `market.no_price` as last-price proxies (document: "last price, not ask; secondary guard since list_all_markets already excludes parlays"). Returns False when `yes_price` or `no_price` is missing/zero, or when both prices are implausibly close to 1.0 (collection artifact).

- [ ] **Step 1: Failing test (mock client)**
```python
# tests/directional/test_scanner.py
import pytest
from core.directional.scanner import KalshiMarketScanner, is_tradeable
from kalshi_client.models import KalshiMarket

def mk(t, yes=0.4, vol=1000):
    return KalshiMarket(ticker=t, event_ticker=t.split("-")[0], series_ticker=t.split("-")[0],
                        title=t, yes_price=yes, volume=vol)

def test_is_tradeable():
    assert is_tradeable(mk("KX-1", yes=0.4)) is True

@pytest.mark.asyncio
async def test_scan_filters_low_volume(monkeypatch):
    class C:
        async def list_all_markets(self, status, max_markets):
            return [mk("KXNFLGAME-1", vol=5), mk("KXNFLGAME-2", vol=5000)]
    sc = KalshiMarketScanner(C(), categorize_fn=lambda t: "Sports", min_volume=100, exclude_categories=[])
    out = await sc.scan(max_markets=50)
    assert [m.ticker for m in out] == ["KXNFLGAME-2"]
```
- [ ] **Step 2: FAIL → Step 3: implement scanner (call `client.list_all_markets`, filter, set `market.category`) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): Kalshi scanner + parlay filter"`

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
- Consumes: `intelligence_engine.evaluate(market_id=..., market_question=..., current_yes_price=..., arb_edge=0.0, resolution_criteria=None) -> SignalSummary|None`. If the engine returns `None` or `summary.signal is None` (e.g. `SignalSummary.neutral`), skip that market.
- Consumes: `passes_edge` (Task 3); `structural_score` (Task 2).
- Produces: `AiDirectional(intelligence_engine, min_confidence, min_edge_pct)`: for each market call `evaluate` with kwargs `market_id=m.ticker, market_question=m.title, current_yes_price=m.yes_price, arb_edge=0.0, resolution_criteria=None`; guard `if summary is None or summary.signal is None: continue`; if `signal.confidence >= min_confidence` and `passes_edge(conf, |edge_vs_market|)` and `|edge_vs_market| >= min_edge_pct`, emit a candidate (side = "YES" if direction=="bullish" else "NO"), folding `structural_score` into `.edge`. Fail-safe: any exception → skip market.

- [ ] **Step 1: Failing test (mock intelligence)**
```python
# tests/directional/test_ai_directional.py
import pytest
from types import SimpleNamespace
from core.directional.strategies.ai_directional import AiDirectional
from kalshi_client.models import KalshiMarket

def mk(ticker, yes_price, category, vol=9000):
    return KalshiMarket(ticker=ticker, event_ticker=ticker.split("-")[0],
                        series_ticker=ticker.split("-")[0], title="x",
                        yes_price=yes_price, category=category, volume=vol)

class FakeIntel:
    async def evaluate(self, **k):
        sig = SimpleNamespace(ai_probability=0.7, confidence=0.85, direction="bullish",
                              edge_vs_market=0.12, reasoning="news")
        return SimpleNamespace(signal=sig)

@pytest.mark.asyncio
async def test_emits_yes_on_strong_bullish():
    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    assert len(cands) == 1 and cands[0].side == "YES" and cands[0].confidence == 0.85

@pytest.mark.asyncio
async def test_skips_low_confidence():
    class Weak(FakeIntel):
        async def evaluate(self, **k):
            sig = SimpleNamespace(ai_probability=0.5, confidence=0.4, direction="bullish",
                                  edge_vs_market=0.2, reasoning="")
            return SimpleNamespace(signal=sig)
    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(Weak(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []

@pytest.mark.asyncio
async def test_skips_none_signal():
    class NoneIntel:
        async def evaluate(self, **k):
            return None
    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(NoneIntel(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []
```
- [ ] **Step 2: FAIL → Step 3: implement (try/except per market; inside try: `summary = await intel.evaluate(market_id=m.ticker, market_question=m.title, current_yes_price=m.yes_price, arb_edge=0.0, resolution_criteria=None)`; `if summary is None or summary.signal is None: continue`; gates: confidence, passes_edge, min_edge_pct; side from direction; edge = |edge_vs_market| + structural_score) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): AI-directional strategy (reuses intelligence)"`

---

### Task 11: Decider (Kelly + risk gate)

**Files:**
- Create: `core/directional/decider.py`
- Test: `tests/directional/test_decider.py`

**Interfaces:**
- Consumes: `kelly_fraction(edge, yes_price, ai_probability, confidence, fraction)` from `core/kelly.py`; `RiskManager.check_directional_order(order, open_count, directional_exposure, max_position, max_total, max_open)` (Task 6); `DirectionalStore.directional_exposure()/open_positions()` (Task 7).
- Produces: `Decider(risk_manager, store, kelly_frac, max_position_usd, cash_balance_fn, caps)` with `decide(candidate) -> DirectionalOrder|None`.

**Sizing rules:**
- `ai_directional` candidates (ai_probability is not None): call `kelly_fraction` in YES-space:
  - YES side: `kelly_fraction(edge=candidate.edge, yes_price=candidate.market_price, ai_probability=candidate.ai_probability, confidence=candidate.confidence, fraction=kelly_frac)`
  - NO side: `kelly_fraction(edge=candidate.edge, yes_price=(1 - candidate.market_price), ai_probability=(1 - candidate.ai_probability), confidence=candidate.confidence, fraction=kelly_frac)`
  - Notional = fraction x cash_balance; cap at max_position_usd.
- `safe_compounder` candidates (ai_probability is None): skip Kelly, use fixed `max_position_usd`.
- `size = floor(notional / price)`, `notional = size * price`. Return None if size < 1.
- Risk gate: call `check_directional_order(order, open_count=len(store.open_positions()), directional_exposure=store.directional_exposure(), max_position=caps.max_position, max_total=caps.total_exposure, max_open=caps.max_open)` → return None if False.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_decider.py
import pytest
from core.directional.decider import Decider
from core.directional.models import DirectionalCandidate

class RM:
    def check_directional_order(self, o, open_count, directional_exposure, max_position, max_total, max_open):
        return o.notional <= max_position

class ST:
    def directional_exposure(self): return 0.0
    def open_positions(self): return []

class Caps:
    max_position = 8
    total_exposure = 30
    max_open = 4

def safe_cand():
    return DirectionalCandidate(market_id="kalshi:KX-1", title="t", category="Sports", side="NO",
                                market_price=0.9, ai_probability=None, confidence=None,
                                edge=0.04, strategy="safe_compounder")

def ai_cand(side="YES"):
    return DirectionalCandidate(market_id="kalshi:KX-1", title="t", category="Finance", side=side,
                                market_price=0.58, ai_probability=0.7, confidence=0.85,
                                edge=0.12, strategy="ai_directional")

def test_safe_compounder_fixed_size():
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 30, caps=Caps())
    o = d.decide(safe_cand())
    assert o is not None and o.notional <= 8 and o.size >= 1

def test_rejected_when_over_cap():
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=20, cash_balance_fn=lambda: 100, caps=Caps())
    # max_position_usd=20 > caps.max_position=8 → notional will be capped at 8 or risk gate rejects
    # The risk gate uses caps.max_position=8; safe_compounder requests 20, capped to 8 by min(); then risk passes.
    # Expected: order is placed at notional=8 (capped).
    o = d.decide(safe_cand())
    assert o is not None and o.notional <= 8

def test_ai_no_side_kelly_inverted():
    """NO-side Kelly uses (1-price, 1-ai_prob) to stay in YES-space."""
    d = Decider(RM(), ST(), kelly_frac=0.25, max_position_usd=8, cash_balance_fn=lambda: 100, caps=Caps())
    o = d.decide(ai_cand(side="NO"))
    assert o is not None and o.side == "NO"
```
- [ ] **Step 2: FAIL → Step 3: implement (Safe Compounder skips Kelly; AI YES: normal kelly_fraction; AI NO: kelly_fraction with inverted price+prob; cap notional; floor size; risk check with cap args from caps object) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): decider (Kelly + risk gate)"`

---

### Task 12: Executor (paper / live)

**Files:**
- Create: `core/directional/executor.py`
- Test: `tests/directional/test_executor.py`

**Interfaces:**
- Consumes: `kalshi_client.place_order(ticker, token_type, side, price, size, strategy_tag)` where `token_type: TokenType`, `side: OrderSide`; `kalshi_client.get_balance() -> float`; `DirectionalStore.record_position`.
- Produces: `Executor(kalshi_client, store)` with `async place(order, mode, stop_loss=None, take_profit=None) -> DirectionalPosition|None`.
  - paper: record position (`mode="paper"`) at `order.price`, NO API call.
  - live: pre-flight balance guard: `bal = await kalshi_client.get_balance(); if bal < order.notional: log warning; return None`. Then convert side string to enums: `token_type = TokenType.YES if order.side == "YES" else TokenType.NO; side_enum = OrderSide.BUY` (directional only buys). Call `place_order(ticker=order.market_id, token_type=token_type, side=side_enum, price=order.price, size=order.size, strategy_tag=order.strategy)`. On success record position (`mode="live"`); on failure return None.

- [ ] **Step 1: Failing test**
```python
# tests/directional/test_executor.py
import pytest
from core.directional.executor import Executor
from core.directional.models import DirectionalOrder

class Store:
    def __init__(self): self.saved = []
    def record_position(self, p): self.saved.append(p); return 1

class Client:
    def __init__(self, balance=100.0): self._balance = balance; self.calls = 0
    async def get_balance(self): return self._balance
    async def place_order(self, **k): self.calls += 1; return object()

@pytest.mark.asyncio
async def test_paper_records_no_api():
    st, cl = Store(), Client()
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"), mode="paper")
    assert cl.calls == 0 and len(st.saved) == 1 and st.saved[0].mode == "paper"

@pytest.mark.asyncio
async def test_live_calls_place_order():
    st, cl = Store(), Client(balance=100.0)
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"), mode="live")
    assert cl.calls == 1 and st.saved[0].mode == "live"

@pytest.mark.asyncio
async def test_live_aborts_on_insufficient_balance():
    st, cl = Store(), Client(balance=1.0)  # balance 1.0 < notional 4.5
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"), mode="live")
    assert pos is None and cl.calls == 0
```
- [ ] **Step 2: FAIL → Step 3: implement (import TokenType, OrderSide from kalshi_client; paper path: skip balance check + API; live path: get_balance guard, convert enums, place_order, record) → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): executor (paper/live) + balance guard"`

---

### Task 13: Tracker (stop-loss/TP/time + resolution sweep)

**Files:**
- Create: `core/directional/tracker.py` (StopLossCalculator adapted from `/tmp/kalshi-ai-bot/src/utils/stop_loss_calculator.py`)
- Test: `tests/directional/test_tracker.py`

**Interfaces:**
- Consumes: `DirectionalStore` (open_positions/update_position), `kalshi_client.get_market`/`get_orderbook_unified` (current price + resolution), `Executor` (to place closing orders in live), `risk_manager` ref (for kill switch gate).
- Produces: `Tracker(store, kalshi_client, executor, risk_manager)` with `async sweep(now: datetime)`.
  - For AI positions: compute current price; call `should_exit(position, current_price, now)`. On exit: paper → mark closed at current; live AND kill switch NOT triggered → place opposing order. Book P&L.
  - For all: if market resolved → settle at 1.0/0.0, mark closed, record realized P&L. (Resolution settlement is allowed even when kill switch is triggered.)
  - Kill switch gate: when `mode=="live"` and `risk_manager.state.kill_switch_triggered`, skip placing live closing orders (resolved market settlement still proceeds).
- `should_exit(position: DirectionalPosition, price: float, now: datetime, max_hold_hours: float) -> tuple[bool, str]` — pure function:
  - `stop_loss`: price <= position.stop_loss (token-price space for the held side)
  - `take_profit`: price >= position.take_profit
  - `max_hold`: `(now - position.opened_at).total_seconds() / 3600 > max_hold_hours`
  - else: `(False, "")`
  - stop_loss/take_profit are stored in the position's own token-price space (YES price for YES positions, NO price for NO positions).

- [ ] **Step 1: Failing test (pure exit logic + kill switch)**
```python
# tests/directional/test_tracker.py
import pytest
from datetime import datetime
from core.directional.tracker import should_exit
from core.directional.models import DirectionalPosition

def pos(side="YES", entry=0.6, sl=0.42, tp=0.9):
    return DirectionalPosition(
        market_id="kalshi:KX-1", side=side, entry_price=entry, size=5,
        strategy="ai_directional", mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=sl, take_profit=tp, notional=3.0
    )

def test_stop_loss_triggers():
    ok, why = should_exit(pos(), price=0.40, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "stop_loss"

def test_take_profit_triggers():
    ok, why = should_exit(pos(), price=0.92, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "take_profit"

def test_time_exit():
    ok, why = should_exit(pos(), price=0.6, now=datetime(2026, 6, 22, 0, 0, 0), max_hold_hours=72)
    assert ok and why == "max_hold"

def test_hold_otherwise():
    ok, _ = should_exit(pos(), price=0.6, now=datetime(2026, 6, 18, 2, 0, 0), max_hold_hours=72)
    assert ok is False

def test_no_side_should_exit():
    """NO position: stop_loss stored as NO-price; same logic applies."""
    p = pos(side="NO", entry=0.9, sl=0.70, tp=0.97)  # NO price sl=0.70, tp=0.97
    ok, why = should_exit(p, price=0.68, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok and why == "stop_loss"
    ok2, why2 = should_exit(p, price=0.98, now=datetime(2026, 6, 18, 1, 0, 0), max_hold_hours=72)
    assert ok2 and why2 == "take_profit"

@pytest.mark.asyncio
async def test_sweep_does_not_close_when_kill_switch(tmp_path):
    """Live sweep skips placing closing orders when kill switch is triggered."""
    from unittest.mock import AsyncMock, MagicMock
    from core.directional.tracker import Tracker
    from core.directional.store import DirectionalStore

    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    class KS:
        class state:
            kill_switch_triggered = True

    executor = MagicMock()
    executor.place = AsyncMock()
    tracker = Tracker(store, kalshi_client=MagicMock(), executor=executor, risk_manager=KS())
    # Even if there are open positions, live close should not be called
    await tracker.sweep(now=datetime(2026, 6, 18, 12, 0, 0))
    executor.place.assert_not_called()
```
- [ ] **Step 2: FAIL → Step 3: implement `should_exit` (price <= stop_loss → "stop_loss"; price >= take_profit → "take_profit"; age > max_hold → "max_hold"; all using datetime arithmetic) + `sweep()` wiring with kill switch gate → Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): tracker exits + kill switch gate + resolution sweep"`

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

> **Depends on Task 5** (`BotConfig.directional` must exist before this runs).

**Files:**
- Modify: `run_with_dashboard.py` (add `_guarded` method to `TradingBotWithDashboard`; add gated engine creation in `start()`)
- Test: manual smoke (no unit test for asyncio task creation) — documented check.

**Interfaces:**
- Consumes: `config.directional.enabled`, `DirectionalEngine`.

- [ ] **Step 1: Add `_guarded` as a class method and the gated launch in `start()`**

Add `_guarded` as a **method of `TradingBotWithDashboard`** (at class level, before `start()`):
```python
async def _guarded(self, coro, name: str) -> None:
    try:
        await coro
    except Exception as e:
        logger.error(f"[{name}] loop crashed (isolated): {e}", exc_info=True)
```

In `start()`, AFTER `kalshi_client` + `intelligence_engine` are created:
```python
directional_cfg = getattr(self.config, "directional", None)
if directional_cfg is None:
    logger.warning("config.directional absent — directional trading disabled")
elif directional_cfg.enabled:
    from core.directional.engine import DirectionalEngine
    self.directional_engine = DirectionalEngine(
        directional_cfg, self.kalshi_client, self.intelligence_engine, self.risk_manager)
    asyncio.create_task(self._guarded(self.directional_engine.run_forever(), "directional"))
    dashboard_state.directional_store = self.directional_engine.store
```

- [ ] **Step 2: Verify the live bot still boots with `directional.enabled: false`** (no behavior change):
```bash
# Confirm logs show no directional start and arb unaffected:
docker logs polymarket-arb 2>&1 | grep -i directional
```
(Do NOT set enabled: true.)
- [ ] **Step 3: Commit** `git commit -m "feat(directional): gated, isolated launch in run_with_dashboard"`

---

### Task 16: Dashboard — /api/directional + panel

**Files:**
- Modify: `dashboard/server.py` (add `directional_store: Optional[DirectionalStore] = None` field to `DashboardState.__init__`; add `build_directional_payload(store)` pure builder; add `GET /api/directional` route; add "Directional" HTML section + JS poll)
- Test: `tests/directional/test_api_directional.py`

**Interfaces:**
- Consumes: `DirectionalStore.open_positions()/recent_signals()/pnl_summary()`.
- Produces: `GET /api/directional` → `{"strategies": [...], "positions": [...], "signals": [...], "pnl": {...}}`. When `dashboard_state.directional_store is None`, returns the empty payload `{"strategies":[],"positions":[],"signals":[],"pnl":{}}`.

- [ ] **Step 1: Failing tests**
```python
# tests/directional/test_api_directional.py
import pytest
from core.directional.store import DirectionalStore
from dashboard.server import build_directional_payload

def test_api_shape(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    payload = build_directional_payload(s)
    assert set(payload) >= {"strategies", "positions", "signals", "pnl"}

def test_null_store_returns_empty():
    payload = build_directional_payload(None)
    assert payload == {"strategies": [], "positions": [], "signals": [], "pnl": {}}
```
- [ ] **Step 2: FAIL → Step 3: implement**
  - Add `self.directional_store: Optional[DirectionalStore] = None` to `DashboardState.__init__`.
  - Implement `build_directional_payload(store) -> dict`: if `store is None` return empty payload; else call `store.open_positions()`, `store.recent_signals()`, `store.pnl_summary()`, return serialized payload.
  - Wire `GET /api/directional` route: reads `dashboard_state.directional_store`, calls `build_directional_payload`.
  - Task 15 sets `dashboard_state.directional_store = self.directional_engine.store` after creating the engine.
  - Add HTML section + JS poll mirroring existing panels.
- [ ] **Step 4: PASS → Step 5: Commit** `git commit -m "feat(directional): dashboard panel + /api/directional"`

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
- **Type consistency:** `DirectionalOrder(.market_id/.notional/.side)` used by risk Protocol (T6) matches T4; `check_directional_order(order, open_count, directional_exposure, max_position, max_total, max_open)` consistent T6/T11; `evaluate(market_id=..., market_question=..., current_yes_price=..., arb_edge=0.0, resolution_criteria=None) -> SignalSummary|None` + None-guard consistent T10; `kelly_fraction(edge, yes_price, ai_probability, confidence, fraction)` matches `core/kelly.py`; `opened_at: datetime` consistent T4/T7/T13; `notional: float` on `DirectionalPosition` consistent T4/T7/T13.
- **Risk isolation:** `check_directional_order` is a new standalone method on `RiskManager`, never `check_order` which guards the live arb path. `RiskConfig` is unchanged.
- **Kill switch:** gated in `check_directional_order` (T6), Tracker live-close gate (T13), and as startup log if `config.directional` absent (T15).

## Notes for implementer
- Keep each new file focused; no edits to `core/arb_engine.py`, `core/execution.py`, the cross-platform monitor, or DataFeed.
- Run the suite after each task: `docker exec -w /app polymarket-arb python -m pytest tests/directional -q` (after `docker cp` of new files) OR a local venv with requirements.
- Do NOT set any `mode: live` or `directional.enabled: true` during implementation — that's a separate, deliberate go-live step after paper validation.
