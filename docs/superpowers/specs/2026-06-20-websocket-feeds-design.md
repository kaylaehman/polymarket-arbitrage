# WebSocket Real-Time Feeds (Sub-project 1) — Design

**Date:** 2026-06-20
**Status:** Approved design, pre-implementation
**Goal:** Replace the live Kalshi-native arb's 30s REST orderbook polling with an event-driven WebSocket feed so it catches fleeting riskless bundle dislocations (YES_ask+NO_ask<$1) in real time — while keeping REST polling as an always-on fallback so the live arb can never go dark.

## Decisions (from brainstorming)
- **Venue:** Kalshi only (PM.US WS deferred — cross-platform is dormant/0 matches).
- **Consumer:** the live Kalshi-native bundle-arb ONLY. Directional (300s, paper) and cross-platform keep current fetching.
- **Architecture: Approach A** — event-driven detection + REST fallback. WS maintains a live book cache; every book update triggers an immediate bundle check on that market; a health supervisor falls back to the existing 30s REST sweep whenever WS is down/stale.
- Must NOT disrupt the live arb ($50, riskless). Gated by a config flag; any WS failure degrades to REST.

## Kalshi WS protocol (grounded)
- URL: `wss://api.elections.kalshi.com/trade-api/ws/v2`.
- Auth: reuse the existing `kalshi_client._auth_headers("GET", "/trade-api/ws/v2")` (RSA-PSS, same scheme as REST) for the connection handshake headers.
- Subscribe: `{"id":<n>,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":[...]}}`.
- Channel `orderbook_delta`: first message per ticker is an `orderbook_snapshot` (full book: yes/no price levels in the dollar/`_fp` format consistent with the REST `get_orderbook` fix), then incremental `orderbook_delta` messages (ticker, side yes/no, price, delta size). Maintain the book by applying deltas to the snapshot.
- Reference template (MIT): `/tmp/kalshi-ai-bot/src/clients/kalshi_ws.py` (connection lifecycle, RSA signing, resubscribe-on-reconnect, per-channel callbacks). Adapt, don't copy wholesale.

## Components
- `kalshi_client/ws.py::KalshiWSClient` — connect/auth/subscribe/unsubscribe; book maintenance from snapshot+delta; auto-reconnect (exponential backoff, capped); resubscribe tracked subscriptions on reconnect; connection-state + last-message-ts exposed; per-update callback hook. Reuses the RSA private key / `_auth_headers` from `kalshi_client`.
- `WSBookCache` (inside ws.py) — `{ticker: OrderBook}` (the unified OrderBook model), `get_book(ticker)`, `last_update_ts`. Books built in the same unified format `get_orderbook_unified` produces (so the bundle math is identical).
- Event-driven detector — on each book update for ticker T: recompute `yes_ask + no_ask`; if it crosses the bundle-long threshold (net of fees + min_edge, using the SAME ArbConfig math as the existing Kalshi arb engine), build the bundle signal and submit it to the EXISTING `kalshi_execution_engine` (reuse arb execution + `risk_manager` path; no duplicated execution). Per-market cooldown (e.g. 5s) to dedup rapid deltas.
- WS supervisor — owns the fallback decision (see below); started/stopped with the Kalshi-native trading loop.

## Reconnection / staleness / fallback (safety core)
- The existing 30s REST sweep in `_run_kalshi_trading` is NEVER removed.
- Health gate: WS `CONNECTED` AND `(now - last_message_ts) < ws_staleness_seconds` (default 10) ⇒ WS drives detection; REST sweep runs at a slow reconciliation cadence (default 120s) to backstop missed updates. Otherwise ⇒ REST sweep resumes full 30s cadence immediately.
- One PRIMARY detection path (WS when healthy); the REST sweep STILL SUBMITS at the reconcile cadence even when WS is primary. Cross-path duplicates are prevented ONLY by the shared `kalshi_arb_engine._opportunity_cooldown` (2s per market+type). Every mode transition is logged with a reason: `[KalshiWS] -> WS primary` / `-> REST fallback (rest:stale|rest:disconnected|rest:disabled)`.
- Reconnect: exponential backoff (e.g. 1s→2s→…→30s cap); on (re)connect, resubscribe the current watched tickers.

## Isolation & safety
- New config `monitoring.kalshi_ws_enabled: true` (default true once shipped; flip to false = exactly today's REST-only behavior) + `monitoring.ws_staleness_seconds: 10` + `monitoring.ws_reconcile_seconds: 120`.
- WS client/detector fully wrapped in try/except; ANY failure logs + degrades to REST fallback — cannot crash the live arb.
- Reuses: existing risk caps, `kalshi_execution_engine`, the liquid watched set from `_select_kalshi_arb_markets()` (WS subscribes to exactly that set; **the watched set is static per process — selected once at startup**), and the bundle math from the Kalshi ArbEngine/ArbConfig. `KalshiWSClient.resubscribe` is used only as the internal reconnect-resubscribe mechanism.
- Does NOT modify the execution engine, risk manager, cross-platform, or directional code.

## Testing (mock WS transport — no live socket)
- Book maintenance: apply a snapshot then a delta sequence; assert the resulting best yes/no bid/ask.
- Bundle detection: feed a delta that makes yes_ask+no_ask < threshold; assert exactly one signal submitted to a fake execution engine; assert cooldown dedups a second rapid delta.
- Subscribe/resubscribe: on simulated reconnect, the tracked tickers are resubscribed.
- Fallback toggle: stale/disconnected WS ⇒ supervisor reports REST-primary; healthy+fresh ⇒ WS-primary; transitions assert correctly.
- Reconnect backoff sequence is bounded/capped.
- One integration test: snapshot+deltas producing a YES+NO<$1 book ⇒ one routed signal; no live API.

## Out of scope (YAGNI)
PM.US WebSocket; feeding WS into directional/cross-platform; other channels (ticker/trade/fill) beyond orderbook_delta; replacing REST entirely.

## Effort
~1 new module (ws.py) + supervisor wiring in `_run_kalshi_trading` + config + ~10-14 tests. Live arb untouched when flag off; REST fallback always present.
