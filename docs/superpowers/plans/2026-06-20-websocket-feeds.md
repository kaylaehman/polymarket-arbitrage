# WebSocket Real-Time Feeds (Sub-project 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the LIVE Kalshi-native bundle arb a real-time WebSocket orderbook feed (event-driven bundle detection) with the existing 30s REST sweep kept as an always-on fallback, so it can never go dark.

**Architecture:** New `kalshi_client/ws.py` (`KalshiWSClient` + book maintenance) connects to Kalshi's `orderbook_delta` channel, maintains a live unified `OrderBook` per watched ticker, and on every update runs the EXISTING `kalshi_arb_engine.analyze()` and submits hits to the EXISTING `kalshi_execution_engine`. A health-gate supervisor in `_run_kalshi_trading` runs WS-primary when healthy and falls back to the REST sweep when WS is down/stale.

**Tech Stack:** Python 3.12 async, `websockets>=12.0` (already in requirements), `cryptography` (existing RSA-PSS), pytest + pytest-asyncio.

## Global Constraints
- MUST NOT disrupt the live arb. The WS path is gated by `monitoring.kalshi_ws_enabled` (default true) and the REST sweep is ALWAYS present as fallback. Any WS exception is caught and degrades to REST.
- Reuse, do NOT reimplement: `kalshi_client._auth_headers("GET", "/trade-api/ws/v2")` (returns `{"KALSHI-ACCESS-KEY","KALSHI-ACCESS-SIGNATURE","KALSHI-ACCESS-TIMESTAMP"}`, signs `{ts_ms}{METHOD}{path}` RSA-PSS); the unified `OrderBook` model (`polymarket_client/models.py`: `OrderBook(market_id, yes, no)`, `.best_ask_yes`, `.best_ask_no`, `TokenOrderBook.bids/asks` = `OrderBookSide(levels=[PriceLevel(price, size)])`); `MarketState(market=Market(market_id, condition_id, question), order_book=ob)`; `self.kalshi_arb_engine.analyze(market_state) -> list[Signal]`; `await self.kalshi_execution_engine.submit_signal(signal)`; the watched set from `self._select_kalshi_arb_markets()`.
- WS URL: `wss://api.elections.kalshi.com/trade-api/ws/v2`. Subscribe: `{"id":<n>,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":[...]}}`.
- Reference (MIT, adapt — do NOT copy wholesale): `/tmp/kalshi-ai-bot/src/clients/kalshi_ws.py`.
- Tests MUST mock the WS transport — NO live socket in tests. Run in the dev venv: `. .venv-dev/bin/activate && python -m pytest tests/test_kalshi_ws.py -q`. Do NOT rebuild/restart the live container during implementation. Work on branch `main` (or a feature branch); confirm `git branch --show-current` first.

## File Structure
- `kalshi_client/ws.py` — `KalshiWSClient`, the book-maintenance helpers (`_BookState`, `apply_snapshot`, `apply_delta`, `book_to_orderbook`), connection/auth/subscribe/reconnect.
- `core/kalshi_ws_detector.py` — `WSBundleDetector` (book-update → MarketState → analyze → submit, with cooldown) and `decide_detection_mode(...)` (pure health-gate).
- Modify: `utils/config_loader.py` (MonitoringConfig fields), `config.yaml` (monitoring block), `run_with_dashboard.py` (`_run_kalshi_trading` wiring).
- Tests: `tests/test_kalshi_ws.py`, `tests/test_kalshi_ws_detector.py`.

---

### Task 1: Config flags

**Files:**
- Modify: `utils/config_loader.py` (MonitoringConfig), `config.yaml` (monitoring block)
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Produces: `config.monitoring.kalshi_ws_enabled: bool = True`, `config.monitoring.ws_staleness_seconds: float = 10.0`, `config.monitoring.ws_reconcile_seconds: float = 120.0`.

- [ ] **Step 1: Failing test**
```python
# tests/test_kalshi_ws.py
from utils.config_loader import MonitoringConfig
def test_ws_config_defaults():
    m = MonitoringConfig()
    assert m.kalshi_ws_enabled is True
    assert m.ws_staleness_seconds == 10.0
    assert m.ws_reconcile_seconds == 120.0
```
- [ ] **Step 2: Run → FAIL** (`AttributeError`). `. .venv-dev/bin/activate && python -m pytest tests/test_kalshi_ws.py::test_ws_config_defaults -q`
- [ ] **Step 3: Implement** — add the three fields to `MonitoringConfig` dataclass (defaults above). Append to `config.yaml` under `monitoring:`:
```yaml
  kalshi_ws_enabled: true       # real-time WS bundle detection (REST sweep stays as fallback)
  ws_staleness_seconds: 10      # WS considered stale if no message within this window -> REST fallback
  ws_reconcile_seconds: 120     # REST reconciliation cadence while WS is healthy
```
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** `git commit -m "feat(ws): config flags for kalshi websocket feed"`

---

### Task 2: Book maintenance from snapshot + delta (pure, no socket)

**Files:**
- Create: `kalshi_client/ws.py`
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Produces:
  - `_BookState` — holds `yes: dict[float, float]` and `no: dict[float, float]` (price→size).
  - `apply_snapshot(state: _BookState, msg: dict) -> None` — reset both sides from `msg["yes"]`/`msg["no"]` (each a list of `[price, size]`).
  - `apply_delta(state: _BookState, msg: dict) -> None` — `state[side][price] += msg["delta"]`; drop the level if resulting size <= 0.
  - `book_to_orderbook(ticker: str, state: _BookState) -> OrderBook` — build the unified `OrderBook` (yes/no `TokenOrderBook`s; Kalshi gives resting BIDS per side, so yes bids = state.yes, no bids = state.no; the unified model derives asks from the opposite side's bids = `1 - price`, mirroring `get_orderbook`/`get_orderbook_unified`). **Read `kalshi_client/api.py::get_orderbook_unified` and replicate exactly how it turns yes_bids/no_bids into the unified OrderBook so the bundle math is identical.**
- **PRICE-UNITS NOTE:** the WS message price units (cents int vs dollar string) MUST be confirmed empirically in Step 0 below; write `apply_snapshot`/`apply_delta` to normalize to the SAME dollar floats `get_orderbook_unified` uses.

- [ ] **Step 0 (empirical — do FIRST): confirm the live WS message format.** We were burned assuming the REST format; do not assume here. Connect to the live WS read-only and dump one snapshot + one delta. Run on docker-services in the container env:
```bash
ssh docker-services 'docker cp kalshi_client/api.py polymarket-arb:/app/kalshi_client/api.py >/dev/null 2>&1; cat > /tmp/wsdump.py' <<'PY'
import asyncio,sys,json,websockets
sys.path.insert(0,"/app")
from utils.config_loader import load_config
from kalshi_client import KalshiClient
async def main():
    cfg=load_config("config.yaml")
    k=KalshiClient(dry_run=True, api_key_id=cfg.api.kalshi_api_key_id, private_key_pem=cfg.api.kalshi_private_key)
    k._load_private_key() if hasattr(k,"_load_private_key") else None
    hdr=k._auth_headers("GET","/trade-api/ws/v2")
    async with websockets.connect("wss://api.elections.kalshi.com/trade-api/ws/v2", additional_headers=hdr) as ws:
        await ws.send(json.dumps({"id":1,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":["KXPRESPARTY-2028-D"]}}))
        for _ in range(6):
            print((await asyncio.wait_for(ws.recv(),timeout=10))[:400])
asyncio.run(main())
PY
ssh docker-services 'docker cp /tmp/wsdump.py polymarket-arb:/tmp/wsdump.py && docker exec -w /app polymarket-arb python3 /tmp/wsdump.py 2>&1 | grep -viE "deprecat"'
```
Record the EXACT message `type` values, the `msg` field names (e.g. `market_ticker`, `yes`, `no`, `price`, `delta`, `side`), and the price units. Write the parser to match what you observe (the field names in the Interfaces above are the expected Kalshi schema — correct them to reality if they differ).

- [ ] **Step 1: Failing test** (using the confirmed schema; example assuming cents-int prices — adjust to reality):
```python
from kalshi_client.ws import _BookState, apply_snapshot, apply_delta, book_to_orderbook
def test_snapshot_then_delta_builds_book():
    st = _BookState()
    apply_snapshot(st, {"market_ticker":"KX-1","yes":[[40,100],[39,50]],"no":[[55,200]]})
    apply_delta(st, {"market_ticker":"KX-1","side":"yes","price":40,"delta":-100})  # remove the 0.40 yes level
    ob = book_to_orderbook("KX-1", st)
    assert ob.yes.best_bid == 0.39          # 0.40 level removed
    # no bid 0.55 -> yes ask 1-0.55 = 0.45
    assert round(ob.best_ask_yes, 2) == 0.45
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `_BookState`, `apply_snapshot`, `apply_delta` (normalizing price units to dollars), and `book_to_orderbook` (mirroring `get_orderbook_unified`'s yes_bids/no_bids → unified OrderBook conversion).
- [ ] **Step 4: Run → PASS.** Add a test that a delta driving a level to size 0 removes it.
- [ ] **Step 5: Commit** `git commit -m "feat(ws): orderbook snapshot+delta book maintenance"`

---

### Task 3: KalshiWSClient (connect/auth/subscribe/reconnect) with injectable transport

**Files:**
- Modify: `kalshi_client/ws.py`
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Consumes: Task 2 helpers; `kalshi_client._auth_headers`.
- Produces: `KalshiWSClient(kalshi_client, on_book_update, connect_fn=websockets.connect)`:
  - `async run(tickers: list[str])` — connect (auth headers from `kalshi_client._auth_headers("GET","/trade-api/ws/v2")`), subscribe to `orderbook_delta` for `tickers`, loop reading messages: route snapshot/delta through Task 2 into a per-ticker `_BookState`, update `self.books[ticker]` (unified OrderBook), set `self.last_message_ts = time.monotonic()`, and `await on_book_update(ticker, self.books[ticker])`.
  - Reconnect with exponential backoff (1,2,4,…,30s cap) on disconnect; resubscribe `self._tickers`.
  - `resubscribe(tickers)` — update subscription set (used when the watched set refreshes).
  - `self.state: str` in {"disconnected","connecting","connected","reconnecting"}; `self.last_message_ts: float`.
  - `connect_fn` is injectable so tests pass a fake async context manager yielding a fake socket (`.send`, `.recv`, async-iterable) — NO real socket in tests.
  - `async stop()`.

- [ ] **Step 1: Failing test** (fake transport):
```python
import asyncio, json, pytest
from kalshi_client.ws import KalshiWSClient
class FakeWS:
    def __init__(self, messages): self._msgs=list(messages); self.sent=[]
    async def send(self, m): self.sent.append(json.loads(m))
    async def recv(self):
        if self._msgs: return self._msgs.pop(0)
        raise asyncio.CancelledError
    async def __aenter__(self): return self
    async def __aexit__(self,*a): return False
@pytest.mark.asyncio
async def test_ws_client_subscribes_and_routes_updates():
    snap = json.dumps({"type":"orderbook_snapshot","msg":{"market_ticker":"KX-1","yes":[[40,100]],"no":[[55,200]]}})
    fake = FakeWS([snap])
    updates=[]
    c = KalshiWSClient(_FakeKalshi(), on_book_update=lambda t,ob: updates.append(t), connect_fn=lambda url, additional_headers=None: fake)
    try: await asyncio.wait_for(c.run(["KX-1"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError): pass
    assert fake.sent and fake.sent[0]["cmd"]=="subscribe" and "KX-1" in fake.sent[0]["params"]["market_tickers"]
    assert updates == ["KX-1"]
# _FakeKalshi: minimal object with _auth_headers(method,path)->{} 
```
(Define a tiny `_FakeKalshi` whose `_auth_headers` returns `{}`. The `on_book_update` may be sync or async — support both with `asyncio.iscoroutine`.)
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `KalshiWSClient.run` (subscribe, read loop, per-ticker book maintenance, callback, last_message_ts), reconnect/backoff (test backoff separately with a connect_fn that raises then succeeds), `resubscribe`, `stop`. Adapt structure from the reference `/tmp/kalshi-ai-bot/src/clients/kalshi_ws.py`.
- [ ] **Step 4: Run → PASS.** Add a test: a `connect_fn` that raises once then returns a fake with a snapshot → assert it reconnected and still routed the update (cap the backoff sleeps via a small injected `sleep_fn`).
- [ ] **Step 5: Commit** `git commit -m "feat(ws): KalshiWSClient connect/subscribe/reconnect"`

---

### Task 4: Event-driven bundle detector

**Files:**
- Create: `core/kalshi_ws_detector.py`
- Test: `tests/test_kalshi_ws_detector.py`

**Interfaces:**
- Consumes: `OrderBook`, `MarketState`, `Market` (`polymarket_client/models.py`); an arb engine with `.analyze(market_state) -> list`; an execution engine with `async submit_signal(signal)`.
- Produces: `WSBundleDetector(arb_engine, execution_engine, market_titles: dict[str,str], cooldown_s: float = 5.0, now_fn=time.monotonic)` with `async on_book_update(ticker: str, ob: OrderBook)`:
  - Build `market_state = MarketState(market=Market(market_id=f"kalshi:{ticker}", condition_id="", question=market_titles.get(ticker,"")), order_book=ob)` (mirror the REST sweep exactly).
  - `signals = arb_engine.analyze(market_state)`; for each signal, if not in cooldown for this ticker, `await execution_engine.submit_signal(signal)` and arm the cooldown.
  - Cooldown: skip submits for a ticker within `cooldown_s` of its last submit.

- [ ] **Step 1: Failing test**
```python
import pytest
from core.kalshi_ws_detector import WSBundleDetector
class FakeArb:
    def __init__(self, sigs): self._sigs=sigs
    def analyze(self, ms): return self._sigs
class FakeExec:
    def __init__(self): self.submitted=[]
    async def submit_signal(self, s): self.submitted.append(s)
@pytest.mark.asyncio
async def test_detector_submits_then_cooldown():
    t=[100.0]
    ex=FakeExec()
    det=WSBundleDetector(FakeArb(["SIG"]), ex, market_titles={"KX-1":"q"}, cooldown_s=5.0, now_fn=lambda: t[0])
    await det.on_book_update("KX-1", _ob())   # _ob(): any OrderBook
    await det.on_book_update("KX-1", _ob())   # within cooldown -> skipped
    assert ex.submitted == ["SIG"]
    t[0]=106.0
    await det.on_book_update("KX-1", _ob())   # cooldown elapsed -> submits again
    assert ex.submitted == ["SIG","SIG"]
@pytest.mark.asyncio
async def test_detector_no_signal_no_submit():
    ex=FakeExec()
    det=WSBundleDetector(FakeArb([]), ex, {"KX-1":"q"})
    await det.on_book_update("KX-1", _ob()); assert ex.submitted==[]
```
(`_ob()` builds a trivial unified `OrderBook` — reuse the model constructors.)
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(ws): event-driven bundle detector + cooldown"`

---

### Task 5: Health-gate (pure) — WS-primary vs REST-fallback

**Files:**
- Modify: `core/kalshi_ws_detector.py`
- Test: `tests/test_kalshi_ws_detector.py`

**Interfaces:**
- Produces: `decide_detection_mode(ws_enabled: bool, ws_state: str, last_message_ts: float|None, now: float, staleness_s: float) -> str` returning `"ws"` or `"rest"`. Returns `"ws"` only when `ws_enabled` and `ws_state == "connected"` and `last_message_ts is not None` and `now - last_message_ts < staleness_s`; else `"rest"`.

- [ ] **Step 1: Failing test**
```python
from core.kalshi_ws_detector import decide_detection_mode
def test_health_gate():
    assert decide_detection_mode(True,"connected",100.0,105.0,10.0)=="ws"
    assert decide_detection_mode(True,"connected",100.0,115.0,10.0)=="rest"  # stale
    assert decide_detection_mode(True,"reconnecting",100.0,101.0,10.0)=="rest"
    assert decide_detection_mode(False,"connected",100.0,101.0,10.0)=="rest" # flag off
    assert decide_detection_mode(True,"connected",None,101.0,10.0)=="rest"   # no msg yet
```
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(ws): health-gate fallback decision"`

---

### Task 6: Wire into `_run_kalshi_trading` (gated, supervisor-driven, REST fallback always present)

**Files:**
- Modify: `run_with_dashboard.py` (`_run_kalshi_trading`)
- Test: manual integration check (documented) — the loop wiring isn't unit-tested; the pieces it composes are (Tasks 2-5).

**Interfaces:**
- Consumes: `KalshiWSClient`, `WSBundleDetector`, `decide_detection_mode`, config flags, `self.kalshi_arb_engine`, `self.kalshi_execution_engine`, `self._select_kalshi_arb_markets`.

- [ ] **Step 1: Implement the gated wiring.** After `watched = await self._select_kalshi_arb_markets()` and the engines exist, add (all inside the existing method, REST sweep retained):
```python
ws_client = None
detector = None
if getattr(self.config.monitoring, "kalshi_ws_enabled", False):
    try:
        from kalshi_client.ws import KalshiWSClient
        from core.kalshi_ws_detector import WSBundleDetector, decide_detection_mode
        titles = {m.ticker: m.title for m in watched}
        detector = WSBundleDetector(self.kalshi_arb_engine, self.kalshi_execution_engine, titles)
        ws_client = KalshiWSClient(self.kalshi_client, on_book_update=detector.on_book_update)
        asyncio.create_task(self._guarded(ws_client.run([m.ticker for m in watched]), "kalshi-ws"))
        logger.info(f"[KalshiWS] real-time feed enabled for {len(watched)} markets")
    except Exception as e:
        logger.error(f"[KalshiWS] failed to start, REST-only: {e}", exc_info=True)
        ws_client = None
```
Then in the existing `while self._running:` sweep, gate the REST cadence on the health decision (REST always runs, but at reconcile cadence when WS is healthy):
```python
import time as _time
from core.kalshi_ws_detector import decide_detection_mode
mode = "rest"
if ws_client is not None:
    mode = decide_detection_mode(True, ws_client.state, ws_client.last_message_ts, _time.monotonic(),
                                 self.config.monitoring.ws_staleness_seconds)
sweep_interval = (self.config.monitoring.ws_reconcile_seconds if mode == "ws" else poll)
# ... existing per-market REST sweep body unchanged ...
await asyncio.sleep(sweep_interval)
```
Log mode transitions (`"[KalshiWS] -> WS primary"` / `"-> REST fallback"`) when `mode` changes between iterations. Reuse the existing `_guarded` helper (added for the directional engine) so a WS crash is isolated. Do NOT remove or alter the REST sweep body, the execution engine, or risk checks.
- [ ] **Step 2: Verify the live bot still boots with the flag and is gated.** `. .venv-dev/bin/activate && python -c "import ast; ast.parse(open('run_with_dashboard.py').read()); print('syntax ok')"`. Run the FULL `tests/` suite (`python -m pytest tests/ -q`) — all green. Do NOT rebuild/restart the container (the controller deploys).
- [ ] **Step 3: Commit** `git commit -m "feat(ws): wire real-time detection into kalshi-native arb with REST fallback"`

---

### Task 7: End-to-end integration test

**Files:**
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Consumes: `KalshiWSClient`, `WSBundleDetector`, a real Kalshi `ArbEngine` (or a thin fake that returns a signal when `best_ask_yes + best_ask_no < 1`).

- [ ] **Step 1: Failing integration test** — drive `KalshiWSClient` (fake transport) with a snapshot then a delta that makes `best_ask_yes + best_ask_no < 1.00`, wired to a `WSBundleDetector` whose arb engine flags that condition and a `FakeExec`; assert exactly ONE signal submitted. (Build the snapshot/deltas so the resulting unified book crosses the bundle threshold.)
- [ ] **Step 2: FAIL → Step 3: make it pass (no new product code expected beyond Tasks 2-5) → Step 4: PASS → Step 5: Commit** `git commit -m "test(ws): end-to-end snapshot+delta -> routed bundle signal"`

---

## Self-Review
- **Spec coverage:** KalshiWSClient + orderbook_delta + snapshot/delta book maintenance (T2,T3); reuse `_auth_headers` (T3); event-driven detect → existing execution (T4); health-gate WS/REST fallback (T5); REST sweep retained + gated cadence + isolation (T6); config flags (T1); mock-WS tests + integration (T2-T7). All spec sections mapped.
- **Empirical guard:** T2 Step 0 confirms the live WS message schema/price-units before writing the parser (the spec's price-units note).
- **Type consistency:** `on_book_update(ticker, ob)` async, used by KalshiWSClient (T3) and implemented by WSBundleDetector (T4); `decide_detection_mode(...)->"ws"|"rest"` (T5) used in T6; `submit_signal`/`analyze` match the REST sweep; `OrderBook.best_ask_yes/best_ask_no` per the model.

## Notes for implementer
- Keep `kalshi_client/ws.py` focused; do NOT modify `core/arb_engine.py`, `core/execution.py`, the cross-platform monitor, or directional code.
- `websockets.connect` kwarg for headers is `additional_headers` in websockets>=12 (older used `extra_headers`) — the installed version is >=12.0, use `additional_headers`; confirm in Step 0.
- Do NOT set `kalshi_ws_enabled` true on a deploy until the controller reviews — though it defaults true, deployment is the controller's step after the final review.
