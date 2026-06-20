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
- **CROSS-PATH DEDUP INVARIANT:** The WS detector and the REST sweep MUST share the SAME `self.kalshi_arb_engine` instance. Its internal `_opportunity_cooldown` (2s per `{market_id}_{opportunity_type}` key, `core/arb_engine.py` line ~462) is the ONLY cross-path dedup guard. `submit_signal` has NO idempotency key. NEVER give the WS detector its own `ArbEngine` instance — doing so would allow both paths to fire on the same opportunity simultaneously and double-execute real orders.

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
  - `_BookState` — holds `yes: dict[float, float]`, `no: dict[float, float]` (price→size), and `last_seq: int | None`. The `last_seq` field tracks the most recently applied sequence number for gap detection (see SEQUENCE-GAP section below).
  - `apply_snapshot(state: _BookState, msg: dict) -> None` — reset `yes`, `no`, AND `last_seq` from the snapshot message. `msg["yes"]`/`msg["no"]` each a list of `[price, size]`; `msg["seq"]` (or the parent frame's `seq`) sets `last_seq`.
  - `apply_delta(state: _BookState, msg: dict) -> None` — `state[side][price] += msg["delta"]`; drop the level if resulting size <= 0. Caller MUST check seq continuity before calling this (see Task 3 routing).
  - `book_to_orderbook(ticker: str, state: _BookState) -> OrderBook` — build the unified `OrderBook` (yes/no `TokenOrderBook`s). Kalshi gives resting BIDS per side: yes bids = state.yes, no bids = state.no. Derive asks from the opposite side's bids as `1 - price`, sorted ASCENDING (lowest ask first, so `best_ask = levels[0]`). Mirror `kalshi_client/models.py::to_unified_orderbook` exactly. **YES asks are derived from NO bids (sorted ascending); NO asks are derived from YES bids (sorted ascending).** The bundle-math in `arb_engine._check_bundle_arbitrage` reads `order_book.best_ask_yes` and `order_book.best_ask_no` which resolve to `yes.asks.levels[0].price` and `no.asks.levels[0].price` — the lowest derived ask is what we need.
- **PRICE-UNITS NOTE:** the WS message price units (cents int vs dollar string) MUST be confirmed empirically in Step 0 below; write `apply_snapshot`/`apply_delta` to normalize to the SAME dollar floats `get_orderbook_unified` uses.

- [ ] **Step 0 (empirical — do FIRST): confirm the live WS message format.** We were burned assuming the REST format; do not assume here. Connect to the live WS read-only and dump one snapshot + one delta. Run on docker-services in the container env:
```bash
cat > /tmp/wsdump.py <<'PY'
import asyncio, sys, json, websockets
sys.path.insert(0, "/app")
from utils.config_loader import load_config
from kalshi_client.api import KalshiClient
async def main():
    cfg = load_config("config.yaml")
    # KalshiClient.__init__ kwargs: dry_run, api_key_id, private_key_pem
    # Config fields: cfg.api.kalshi_api_key_id, cfg.api.kalshi_private_key
    k = KalshiClient(
        dry_run=True,
        api_key_id=cfg.api.kalshi_api_key_id,
        private_key_pem=cfg.api.kalshi_private_key,
    )
    # _auth_headers loads the private key internally via _load_private_key()
    hdr = k._auth_headers("GET", "/trade-api/ws/v2")
    # NOTE: the path "/trade-api/ws/v2" is passed VERBATIM to _auth_headers —
    # do NOT prefix it with the REST base URL. The signing scheme expects exactly
    # this path string.
    async with websockets.connect(
        "wss://api.elections.kalshi.com/trade-api/ws/v2",
        additional_headers=hdr
    ) as ws:
        await ws.send(json.dumps({
            "id": 1, "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_tickers": ["KXPRESPARTY-2028-D"]}
        }))
        for _ in range(6):
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            print(raw[:600])
asyncio.run(main())
PY
ssh docker-services 'docker cp /tmp/wsdump.py polymarket-arb:/tmp/wsdump.py && docker exec -w /app polymarket-arb python3 /tmp/wsdump.py 2>&1 | grep -viE "deprecat"'
```

Record from the output:
- The EXACT `type` field values on each frame (e.g. `"orderbook_snapshot"`, `"orderbook_delta"`, `"subscribed"`)
- The top-level `seq` field — present on snapshot and delta frames, absent on ack frames
- The `msg` inner body field names: `market_ticker`, `yes`, `no` (lists of `[price, size]`), `side`, `price`, `delta`
- **Price units**: are prices ints (cents, e.g. `40`) or floats/strings (dollars, e.g. `0.40`)? Normalize to dollar floats.
- **Delta semantics**: does `delta` represent a cumulative-total (replace the level) or an incremental-change (add to existing)? How is a level deleted — `delta` of 0, negative resulting size, or a special sentinel?
- The `seq` field's location: is it in the outer frame or inside `msg`? Confirm whether `seq` increments by 1 per delta or by some other step.

Write `apply_snapshot`/`apply_delta` to match what you observe. The field names in the Interfaces above are the expected Kalshi schema — correct them to reality if they differ.

- [ ] **Step 1: Failing tests** (using the confirmed schema; example assuming dollar-float prices — adjust to reality):
```python
from kalshi_client.ws import _BookState, apply_snapshot, apply_delta, book_to_orderbook

def test_snapshot_then_delta_builds_book():
    st = _BookState()
    # Inner msg bodies (as returned by frame["msg"])
    apply_snapshot(st, {"market_ticker": "KX-1", "yes": [[0.40, 100], [0.39, 50]], "no": [[0.55, 200]], "seq": 1})
    apply_delta(st, {"market_ticker": "KX-1", "side": "yes", "price": 0.40, "delta": -100, "seq": 2})
    ob = book_to_orderbook("KX-1", st)
    assert ob.yes.best_bid == 0.39          # 0.40 level removed by delta
    # no bid 0.55 -> yes ask 1-0.55 = 0.45
    assert round(ob.best_ask_yes, 2) == 0.45

def test_multi_level_asks_sorted_ascending():
    """best_ask_yes must be the LOWEST derived ask (asks sorted ascending)."""
    st = _BookState()
    # Two NO bids: 0.30 and 0.50. Derived YES asks: 0.70 and 0.50.
    # After sorting ascending: [0.50, 0.70]. best_ask_yes = 0.50 (lowest).
    apply_snapshot(st, {"market_ticker": "KX-2", "yes": [], "no": [[0.30, 100], [0.50, 200]], "seq": 1})
    ob = book_to_orderbook("KX-2", st)
    assert round(ob.best_ask_yes, 2) == 0.50  # lowest derived ask, not 0.70
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `_BookState`, `apply_snapshot`, `apply_delta` (normalizing price units to dollars), and `book_to_orderbook` (mirroring `to_unified_orderbook`'s yes_bids/no_bids → unified OrderBook conversion; sort derived asks ascending, sort bids descending).
- [ ] **Step 4: Run → PASS.** Also add: (a) a test that a delta driving a level to size <= 0 removes it; (b) the `test_multi_level_asks_sorted_ascending` test above.
- [ ] **Step 5: Commit** `git commit -m "feat(ws): orderbook snapshot+delta book maintenance"`

---

### Task 3: KalshiWSClient (connect/auth/subscribe/reconnect) with injectable transport

**Files:**
- Modify: `kalshi_client/ws.py`
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Consumes: Task 2 helpers; `kalshi_client._auth_headers`.
- Produces: `KalshiWSClient(kalshi_client, on_book_update, connect_fn=websockets.connect)`:
  - `async run(tickers: list[str])` — connect (auth headers from `kalshi_client._auth_headers("GET", "/trade-api/ws/v2")`), subscribe to `orderbook_delta` for `tickers`, then loop reading frames.

  **Frame routing (envelope unwrap):** Kalshi frames have the envelope structure:
  ```json
  {"type": "orderbook_snapshot|orderbook_delta", "seq": N, "msg": {...inner body...}}
  ```
  The read loop:
  1. Parses each raw JSON frame.
  2. Routes on `frame["type"]`: `"orderbook_snapshot"` → apply_snapshot; `"orderbook_delta"` → apply_delta (after seq check); subscription acks / errors / other types → log and ignore.
  3. Passes `frame["msg"]` (the INNER body, NOT the full frame) to `apply_snapshot`/`apply_delta`. These functions receive and parse the inner body only.

  **Sequence-gap handling (live-money safety):** Each orderbook frame carries a top-level `seq` field. `_BookState.last_seq` tracks the last applied seq.
  - **RULE:** Before applying a delta: if `frame["seq"] != state.last_seq + 1`, a gap has occurred (dropped frame → corrupted book → phantom YES+NO<$1 → real orders placed on a lie). DROP the `_BookState` for that ticker (set to a fresh `_BookState()`) and mark it "awaiting snapshot" by leaving `last_seq = None`. Do NOT emit a book update. Do NOT run detection for that ticker until a fresh `orderbook_snapshot` rebuilds it.
  - Also, if an `orderbook_snapshot` arrives mid-stream (after the initial one), treat it as a full reset: apply it, set `last_seq`, and resume normally.
  - After successfully applying a snapshot or delta, update `state.last_seq = frame["seq"]`.

  Per-ticker `_BookState` lives in `self._states: dict[str, _BookState]`. On a successful update: set `self.books[ticker]` (unified `OrderBook`), set `self.last_message_ts = time.monotonic()`, and `await on_book_update(ticker, self.books[ticker])`.

  **`on_book_update` is ALWAYS async.** `KalshiWSClient` always calls `await on_book_update(ticker, ob)`. Do NOT add "sync or async" dual-support via `asyncio.iscoroutine` — that pattern silently drops the call if the callback is accidentally sync on the live submit path.

  <!-- NOTE: the WS path "/trade-api/ws/v2" is passed VERBATIM to _auth_headers
       (not prefixed with the REST base URL). Do NOT "fix" it. The signing scheme
       expects exactly this path string to match the REST signing scheme. -->

  - Reconnect with exponential backoff (1,2,4,…,30s cap) on disconnect; resubscribe `self._tickers` on reconnect. `resubscribe` is the internal reconnect-resubscribe mechanism only — the watched set is STATIC per process (selected once in `_run_kalshi_trading`).
  - `self.state: str` in {"disconnected","connecting","connected","reconnecting"}; `self.last_message_ts: float | None`.
  - `connect_fn` is injectable so tests pass a fake async context manager yielding a fake socket (`.send`, `.recv`, async-iterable) — NO real socket in tests.
  - `async stop()`.

- [ ] **Step 1: Failing tests** (fake transport — note: `on_book_update` is async):
```python
import asyncio, json, pytest
from kalshi_client.ws import KalshiWSClient

class FakeWS:
    def __init__(self, messages): self._msgs = list(messages); self.sent = []
    async def send(self, m): self.sent.append(json.loads(m))
    async def recv(self):
        if self._msgs: return self._msgs.pop(0)
        raise asyncio.CancelledError
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

class _FakeKalshi:
    def _auth_headers(self, method, path): return {}

@pytest.mark.asyncio
async def test_ws_client_subscribes_and_routes_updates():
    # Full envelope (outer frame with type/seq/msg)
    snap = json.dumps({
        "type": "orderbook_snapshot",
        "seq": 1,
        "msg": {"market_ticker": "KX-1", "yes": [[0.40, 100]], "no": [[0.55, 200]]}
    })
    fake = FakeWS([snap])
    updates = []

    async def on_update(t, ob): updates.append(t)

    c = KalshiWSClient(_FakeKalshi(), on_book_update=on_update, connect_fn=lambda url, additional_headers=None: fake)
    try:
        await asyncio.wait_for(c.run(["KX-1"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    assert fake.sent and fake.sent[0]["cmd"] == "subscribe" and "KX-1" in fake.sent[0]["params"]["market_tickers"]
    assert updates == ["KX-1"]
    # Verify the book was built from the inner msg body
    assert c.books["KX-1"] is not None

@pytest.mark.asyncio
async def test_seq_gap_drops_book():
    """A delta with seq != last_seq+1 must drop the book state (no update emitted,
    best_ask_yes is None) until a fresh snapshot arrives.

    Rationale: a dropped WS frame means we missed one or more order events. The
    locally-maintained book is now stale. If we continue applying subsequent
    deltas on a corrupt book, the derived YES ask or NO ask may appear artificially
    low — triggering a phantom bundle-long signal (YES_ask + NO_ask < $1) and
    placing real orders on prices that no longer exist on the exchange.
    """
    snap = json.dumps({"type": "orderbook_snapshot", "seq": 1,
                        "msg": {"market_ticker": "KX-1", "yes": [[0.40, 100]], "no": [[0.55, 200]]}})
    # seq=3, but last_seq was 1 -> gap (expected 2)
    bad_delta = json.dumps({"type": "orderbook_delta", "seq": 3,
                             "msg": {"market_ticker": "KX-1", "side": "yes", "price": 0.40, "delta": -100}})
    fake = FakeWS([snap, bad_delta])
    updates = []

    async def on_update(t, ob): updates.append((t, ob))

    c = KalshiWSClient(_FakeKalshi(), on_book_update=on_update, connect_fn=lambda url, additional_headers=None: fake)
    try:
        await asyncio.wait_for(c.run(["KX-1"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    # Snapshot fires one update; bad delta must NOT fire a second update
    assert len(updates) == 1
    # After gap, book state is awaiting-snapshot: best_ask_yes is None
    assert c.books.get("KX-1") is None or c.books["KX-1"].best_ask_yes is None

@pytest.mark.asyncio
async def test_envelope_unwrap_updates_book():
    """Feed a FULL envelope; assert the book updates correctly (proves unwrap works)."""
    snap = json.dumps({
        "type": "orderbook_snapshot", "seq": 1,
        "msg": {"market_ticker": "KX-2", "yes": [[0.60, 50]], "no": [[0.35, 80]]}
    })
    fake = FakeWS([snap])
    updates = []

    async def on_update(t, ob): updates.append((t, ob))

    c = KalshiWSClient(_FakeKalshi(), on_book_update=on_update, connect_fn=lambda url, additional_headers=None: fake)
    try:
        await asyncio.wait_for(c.run(["KX-2"]), timeout=1)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    assert updates, "expected at least one book update"
    ticker, ob = updates[0]
    assert ticker == "KX-2"
    # no bid 0.35 -> yes ask 1-0.35 = 0.65
    assert round(ob.best_ask_yes, 2) == 0.65
```
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `KalshiWSClient.run` (subscribe, read loop with envelope unwrap + seq-gap routing, per-ticker `_BookState`, book maintenance, async callback, `last_message_ts`), reconnect/backoff, `resubscribe`, `stop`. Adapt structure from the reference `/tmp/kalshi-ai-bot/src/clients/kalshi_ws.py`.

  Task 2's `apply_snapshot`/`apply_delta` take the INNER `msg` body. Task 3's read loop passes `frame["msg"]` to them. This boundary is explicit: tests in Task 2 pass inner bodies; tests in Task 3 pass full envelopes to `run()`.

- [ ] **Step 4: Run → PASS.** Add a reconnect test: a `connect_fn` that raises once then returns a fake with a snapshot → assert it reconnected and still routed the update (cap backoff sleeps via an injected `sleep_fn`).
- [ ] **Step 5: Commit** `git commit -m "feat(ws): KalshiWSClient connect/subscribe/reconnect with seq-gap protection"`

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
  - **DEDUP NOTE:** This per-ticker `cooldown_s` dedup is local to `WSBundleDetector`. Cross-path dedup (WS vs REST) relies SOLELY on the shared `self.kalshi_arb_engine._opportunity_cooldown` (2s per `{market_id}_{opportunity_type}` in `core/arb_engine.py`). `WSBundleDetector` MUST receive the SAME `self.kalshi_arb_engine` instance that the REST sweep uses (see Task 6 wiring). There is no idempotency key in `submit_signal`.

- [ ] **Step 1: Failing test**
```python
import pytest
from core.kalshi_ws_detector import WSBundleDetector

class FakeArb:
    def __init__(self, sigs): self._sigs = sigs
    def analyze(self, ms): return self._sigs

class FakeExec:
    def __init__(self): self.submitted = []
    async def submit_signal(self, s): self.submitted.append(s)

@pytest.mark.asyncio
async def test_detector_submits_then_cooldown():
    t = [100.0]
    ex = FakeExec()
    det = WSBundleDetector(FakeArb(["SIG"]), ex, market_titles={"KX-1": "q"}, cooldown_s=5.0, now_fn=lambda: t[0])
    await det.on_book_update("KX-1", _ob())   # submits
    await det.on_book_update("KX-1", _ob())   # within cooldown -> skipped
    assert ex.submitted == ["SIG"]
    t[0] = 106.0
    await det.on_book_update("KX-1", _ob())   # cooldown elapsed -> submits again
    assert ex.submitted == ["SIG", "SIG"]

@pytest.mark.asyncio
async def test_detector_no_signal_no_submit():
    ex = FakeExec()
    det = WSBundleDetector(FakeArb([]), ex, {"KX-1": "q"})
    await det.on_book_update("KX-1", _ob())
    assert ex.submitted == []
```
(`_ob()` builds a trivial unified `OrderBook` — reuse the model constructors.)
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(ws): event-driven bundle detector + cooldown"`

---

### Task 5: Health-gate (pure) — WS-primary vs REST-fallback

**Files:**
- Modify: `core/kalshi_ws_detector.py`
- Test: `tests/test_kalshi_ws_detector.py`

**Interfaces:**
- Produces: `decide_detection_mode(ws_enabled: bool, ws_state: str, last_message_ts: float|None, now: float, staleness_s: float) -> tuple[str, str]` returning `(mode, reason)` where `mode` is `"ws"` or `"rest"` and `reason` is one of `"ws"` | `"rest:stale"` | `"rest:disconnected"` | `"rest:disabled"`.
  - Returns `("ws", "ws")` only when `ws_enabled` and `ws_state == "connected"` and `last_message_ts is not None` and `now - last_message_ts < staleness_s`.
  - Returns `("rest", "rest:disabled")` when `not ws_enabled`.
  - Returns `("rest", "rest:disconnected")` when `ws_enabled` and `ws_state != "connected"`.
  - Returns `("rest", "rest:stale")` when `ws_enabled` and `ws_state == "connected"` but stale or no message yet.

- [ ] **Step 1: Failing test**
```python
from core.kalshi_ws_detector import decide_detection_mode

def test_health_gate():
    assert decide_detection_mode(True, "connected", 100.0, 105.0, 10.0) == ("ws", "ws")
    assert decide_detection_mode(True, "connected", 100.0, 115.0, 10.0) == ("rest", "rest:stale")
    assert decide_detection_mode(True, "reconnecting", 100.0, 101.0, 10.0) == ("rest", "rest:disconnected")
    assert decide_detection_mode(False, "connected", 100.0, 101.0, 10.0) == ("rest", "rest:disabled")
    assert decide_detection_mode(True, "connected", None, 101.0, 10.0) == ("rest", "rest:stale")
```
- [ ] **Step 2: FAIL → Step 3: implement → Step 4: PASS → Step 5: Commit** `git commit -m "feat(ws): health-gate fallback decision with reason"`

---

### Task 6: Wire into `_run_kalshi_trading` (gated, supervisor-driven, REST fallback always present)

**Files:**
- Modify: `run_with_dashboard.py` (`_run_kalshi_trading`)
- Test: manual integration check (documented) — the loop wiring isn't unit-tested; the pieces it composes are (Tasks 2-5).

**Interfaces:**
- Consumes: `KalshiWSClient`, `WSBundleDetector`, `decide_detection_mode`, config flags, `self.kalshi_arb_engine`, `self.kalshi_execution_engine`, `self._select_kalshi_arb_markets`.
- **DEDUP REQUIREMENT:** `WSBundleDetector` MUST receive `self.kalshi_arb_engine` — the same engine instance used by the REST sweep. This is the only cross-path dedup mechanism (the engine's `_opportunity_cooldown`). Never construct a new `ArbEngine` here.

- [ ] **Step 1: Implement the gated wiring.** After `watched = await self._select_kalshi_arb_markets()` and the engines exist, add (all inside the existing method, REST sweep retained):
```python
ws_client = None
detector = None
if getattr(self.config.monitoring, "kalshi_ws_enabled", False):
    try:
        from kalshi_client.ws import KalshiWSClient
        from core.kalshi_ws_detector import WSBundleDetector, decide_detection_mode
        titles = {m.ticker: m.title for m in watched}
        # MUST use self.kalshi_arb_engine — shared instance for cross-path dedup
        detector = WSBundleDetector(self.kalshi_arb_engine, self.kalshi_execution_engine, titles)
        ws_client = KalshiWSClient(self.kalshi_client, on_book_update=detector.on_book_update)
        asyncio.create_task(self._guarded(ws_client.run([m.ticker for m in watched]), "kalshi-ws"))
        logger.info(f"[KalshiWS] real-time feed enabled for {len(watched)} markets")
    except Exception as e:
        logger.error(f"[KalshiWS] failed to start, REST-only: {e}", exc_info=True)
        ws_client = None
```
Then in the existing `while self._running:` sweep, gate the REST cadence on the health decision. Carry a `prev_mode` local so the transition log fires only on change:
```python
import time as _time
from core.kalshi_ws_detector import decide_detection_mode
prev_mode = None
# (inside the while loop:)
mode = "rest"
if ws_client is not None:
    mode, reason = decide_detection_mode(
        True, ws_client.state, ws_client.last_message_ts,
        _time.monotonic(), self.config.monitoring.ws_staleness_seconds
    )
    if mode != prev_mode:
        if mode == "ws":
            logger.info("[KalshiWS] -> WS primary")
        else:
            logger.info(f"[KalshiWS] -> REST fallback ({reason})")
        prev_mode = mode
sweep_interval = (self.config.monitoring.ws_reconcile_seconds if mode == "ws" else poll)
# ... existing per-market REST sweep body unchanged ...
await asyncio.sleep(sweep_interval)
```
Note: the REST sweep STILL RUNS every iteration regardless of mode — only the `sweep_interval` changes (120s when WS is healthy, `poll` seconds when REST is primary). Cross-path duplicates are prevented only by the shared engine's `_opportunity_cooldown`.

- [ ] **Step 2: Verify the live bot still boots with the flag and is gated.** `. .venv-dev/bin/activate && python -c "import ast; ast.parse(open('run_with_dashboard.py').read()); print('syntax ok')"`. Run the FULL `tests/` suite (`python -m pytest tests/ -q`) — all green. Do NOT rebuild/restart the container (the controller deploys).
- [ ] **Step 3: Commit** `git commit -m "feat(ws): wire real-time detection into kalshi-native arb with REST fallback"`

---

### Task 7: End-to-end integration test

**Files:**
- Test: `tests/test_kalshi_ws.py`

**Interfaces:**
- Consumes: `KalshiWSClient`, `WSBundleDetector`, a real Kalshi `ArbEngine` (or a thin fake that returns a signal when `best_ask_yes + best_ask_no < 1`).

- [ ] **Step 1: Failing integration test** — drive `KalshiWSClient` (fake transport) with a snapshot then a delta that makes `best_ask_yes + best_ask_no < 1.00`, wired to a `WSBundleDetector` whose arb engine flags that condition and a `FakeExec`; assert exactly ONE signal submitted. (Build the snapshot/deltas so the resulting unified book crosses the bundle threshold.)

- [ ] **Step 2: Failing duplicate-suppression test** — verify the shared engine cooldown is the cross-path dedup:
```python
@pytest.mark.asyncio
async def test_duplicate_suppression_via_shared_engine():
    """Submit a bundle through WSBundleDetector, then call the SAME engine's
    analyze() again within 2 seconds. The engine's _opportunity_cooldown must
    suppress the second signal. This proves that sharing one ArbEngine instance
    deduplicates across WS and REST paths — since submit_signal has no
    idempotency key, the engine cooldown is the ONLY guard.
    """
    from core.arb_engine import ArbEngine, ArbConfig
    from core.kalshi_ws_detector import WSBundleDetector
    # ... build a MarketState with best_ask_yes + best_ask_no < 1 - min_edge ...
    engine = ArbEngine(ArbConfig(min_edge=0.01))
    ex = FakeExec()
    det = WSBundleDetector(engine, ex, {"KX-1": "q"}, cooldown_s=0.0)  # WS cooldown=0 so only engine cooldown acts
    ms = _market_state_with_arb("KX-1")  # helper: builds a MarketState that will produce a signal
    # First: run through WS detector path
    await det.on_book_update("KX-1", ms.order_book)
    assert len(ex.submitted) == 1
    # Second: call engine.analyze directly (simulates REST sweep hitting within 2s cooldown)
    signals = engine.analyze(ms)
    assert signals == [], "engine cooldown must suppress second signal within 2s"
```
- [ ] **Step 3: FAIL → Step 4: make tests pass (no new product code expected beyond Tasks 2-6) → Step 5: PASS → Step 6: Commit** `git commit -m "test(ws): end-to-end snapshot+delta -> routed bundle signal + dedup guard"`

---

## Self-Review
- **Spec coverage:** KalshiWSClient + orderbook_delta + snapshot/delta book maintenance (T2,T3); reuse `_auth_headers` (T3); event-driven detect → existing execution (T4); health-gate WS/REST fallback with reason (T5); REST sweep retained + gated cadence + isolation (T6); config flags (T1); mock-WS tests + integration (T2-T7). All spec sections mapped.
- **Empirical guard:** T2 Step 0 confirms the live WS message schema/price-units/delta-semantics before writing the parser.
- **Type consistency:** `on_book_update(ticker, ob)` always `async`, defined by `WSBundleDetector` (T4), always `await`-ed by `KalshiWSClient` (T3); `decide_detection_mode(...) -> tuple[str,str]` (T5) used in T6 with `mode, reason = ...`; `submit_signal`/`analyze` match the REST sweep; `OrderBook.best_ask_yes/best_ask_no` per the model; `_BookState.last_seq: int | None` threaded through T2-T3.
- **Detection path:** one PRIMARY detection path (WS when healthy). The REST sweep STILL SUBMITS at the reconcile cadence even when WS is primary; cross-path duplicates are prevented ONLY by the shared engine `_opportunity_cooldown` (2s, T7 test verifies this). No "exactly one path" — both paths are always active, just at different cadences.
- **Watched set:** static per process — selected once in `_run_kalshi_trading`. `KalshiWSClient.resubscribe` is used only for reconnect-resubscribe, not for refreshing the watched set.
- **Seq-gap safety:** T3 routing drops and resets `_BookState` on seq gaps. T3 test `test_seq_gap_drops_book` exercises this. Without it, a dropped WS frame → corrupt book → phantom arb signal → real orders.
- **Envelope boundary:** T2 tests pass inner `msg` bodies. T3 tests pass full envelopes to `run()`. The unwrap (`frame["msg"]` → `apply_snapshot`/`apply_delta`) is explicit and tested in `test_envelope_unwrap_updates_book`.
- **Ask sort:** `book_to_orderbook` sorts derived asks ascending (lowest first). `test_multi_level_asks_sorted_ascending` (T2) verifies `best_ask_yes` = lowest derived ask.

## Notes for implementer
- Keep `kalshi_client/ws.py` focused; do NOT modify `core/arb_engine.py`, `core/execution.py`, the cross-platform monitor, or directional code.
- `websockets.connect` kwarg for headers is `additional_headers` in websockets>=12 (older used `extra_headers`) — the installed version is >=12.0, use `additional_headers`; confirm in Step 0.
- Do NOT set `kalshi_ws_enabled` true on a deploy until the controller reviews — though it defaults true, deployment is the controller's step after the final review.
- `KalshiClient.__init__` kwargs: `dry_run`, `api_key_id`, `private_key_pem`. Config fields: `cfg.api.kalshi_api_key_id`, `cfg.api.kalshi_private_key`. `_auth_headers` lazily calls `_load_private_key()` internally — do NOT call `_load_private_key()` separately.
