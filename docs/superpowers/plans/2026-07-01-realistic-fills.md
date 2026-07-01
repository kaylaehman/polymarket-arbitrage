# Realistic Maker Fills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Make PAPER maker-order fills reflect reality so paper P&L predicts live. Today
paper records a `maker_longshot` order as instantly OPEN at `post_price` (100% fill at a
favorable price) — adversely biased (winners' asks run away and never fill; losers' asks
come to you). Model fills from the real Kalshi orderbook instead, and report the fill rate.

**Architecture:** Paper maker orders record as `pending`; the tracker's pending sweep
checks the live Kalshi orderbook each cycle and (a) promotes to `open` if the market's NO
ask reached `<= post_price` (would have filled), or (b) marks `unfilled` at TTL (never a
real trade — excluded from P&L/win-rate). A fill-rate report exposes filled/unfilled and
win-rate-among-filled.

**Tech Stack:** Python 3.12, sqlite, pytest asyncio-auto.

## Global Constraints
- PAPER path only — do NOT change live maker behavior (`order_id` present).
- `unfilled` positions are NOT trades: excluded from closed-position P&L/win-rate stats
  (those query `status='closed'`). Use a distinct `status='unfilled'`.
- Never raise into the sweep: orderbook fetch failure -> leave pending (retry next cycle).
- Test command: `export PYTHONPYCACHEPREFIX=/tmp/fills && .venv-dev/bin/pytest <path> -q -p no:cacheprovider`
- Fill condition for a NO-BUY resting at `post_price` (= position.entry_price): the current
  NO ask `<= post_price`. Read it from `get_orderbook_unified(ticker).no.best_ask`.

---

### Task 1: Paper maker orders record as `pending`, not instant-open

**Files:**
- Modify: `core/directional/executor.py` (the `_handle_maker` paper branch, ~line 143)
- Test: `tests/directional/test_executor_paper_maker_pending.py`

**Interfaces:**
- Produces: a paper `maker_longshot` order recorded with `status="pending"` (no order_id),
  entry_price = post_price. (Live path unchanged.)

- [ ] **Step 1:** Write a failing test: build an Executor in paper mode, call the maker
  placement for a NO order at post_price 0.9, assert the recorded position has
  `status == "pending"` (currently it's "open"). Use the existing test doubles in
  `tests/directional/` for the store/client (mirror `test_executor` if present).
- [ ] **Step 2:** Run it, verify it fails (status is "open").
- [ ] **Step 3:** In `executor.py` `_handle_maker`, change the paper branch from
  `status="open"` to `status="pending"`:
  ```python
  if mode == "paper":
      return self._record(order, mode, stop_loss, take_profit, status="pending")
  ```
- [ ] **Step 4:** Run green. Also run `tests/directional/test_executor*.py -q -p no:cacheprovider`.
- [ ] **Step 5:** Commit (`feat(fills): paper maker orders start pending (realistic fill)`). Commit only executor.py + the new test.

---

### Task 2: Tracker fills paper maker orders from the real orderbook

**Files:**
- Modify: `core/directional/tracker.py` (`_check_pending_maker`, ~line 420)
- Test: `tests/directional/test_paper_maker_fill.py`

**Interfaces:**
- Consumes: `pos.mode`, `pos.entry_price` (= post_price), `self._client.get_orderbook_unified(ticker)`
  whose returned OrderBook has `.no.best_ask` (float or None).
- Produces: pending paper position -> `open` when `no_ask <= post_price`; -> `unfilled`
  (new status) when age > `order_ttl_minutes` and never filled.

- [ ] **Step 1:** Write failing tests:
  - A pending paper position (mode="paper", order_id=None, entry_price=0.90); mock
    `get_orderbook_unified` to return an OrderBook whose `no.best_ask = 0.88` (<= 0.90)
    -> after `_check_pending_maker`, position status == "open" (filled).
  - Same but `no.best_ask = 0.95` (> 0.90) and age > TTL -> status == "unfilled".
  - Same but `no.best_ask = 0.95` and age < TTL -> still "pending".
- [ ] **Step 2:** Run, verify they fail (paper is currently skipped: `if pos.order_id is None: return`).
- [ ] **Step 3:** In `_check_pending_maker`, before the `if pos.order_id is None: return`,
  add a paper branch:
  ```python
  if pos.order_id is None:
      if getattr(pos, "mode", None) == "paper":
          await self._check_paper_maker_fill(pos, now, order_ttl_minutes)
      return
  ```
  and add the method:
  ```python
  async def _check_paper_maker_fill(self, pos, now, order_ttl_minutes):
      """Model a resting paper NO-buy: fills iff the real NO ask reached <= post_price;
      else cancelled 'unfilled' at TTL. post_price is pos.entry_price. Never raises."""
      ticker = pos.market_id.split(":", 1)[-1]
      no_ask = None
      try:
          ob = await self._client.get_orderbook_unified(ticker)
          no_ask = getattr(getattr(ob, "no", None), "best_ask", None) if ob else None
      except Exception as exc:
          logger.debug("paper-fill orderbook fetch failed for %s: %s", ticker, exc)
          return  # leave pending, retry next cycle
      if no_ask is not None and no_ask <= pos.entry_price:
          self._store.update_position(pos.market_id, status="open")
          logger.info("[paper-fill] %s filled (no_ask %.2f <= post %.2f)", pos.market_id, no_ask, pos.entry_price)
          return
      # TTL: never filled -> not a trade
      now_naive = now.replace(tzinfo=None) if now.tzinfo else now
      op = pos.opened_at.replace(tzinfo=None) if pos.opened_at.tzinfo else pos.opened_at
      if (now_naive - op).total_seconds() / 60.0 > order_ttl_minutes:
          self._store.update_position(pos.market_id, status="unfilled", closed_at=now.isoformat())
          logger.info("[paper-fill] %s unfilled at TTL (no_ask never <= post %.2f)", pos.market_id, pos.entry_price)
  ```
- [ ] **Step 4:** Run green; run `tests/directional/test_tracker.py -q -p no:cacheprovider` (no regressions).
- [ ] **Step 5:** Commit (`feat(fills): tracker fills paper maker orders from real orderbook`). Commit only tracker.py + the new test.

---

### Task 3: Fill-rate report

**Files:**
- Modify: `core/directional/store.py` (add `maker_fill_stats()`)
- Create: `scripts/fill_rate_report.py`
- Test: `tests/directional/test_fill_stats.py`

**Interfaces:**
- Produces: `DirectionalStore.maker_fill_stats(strategy="maker_longshot") -> {pending, filled_open, filled_settled, unfilled, fill_rate, win_rate_filled}`
  where fill_rate = (open+closed) / (open+closed+unfilled) over maker positions, and
  win_rate_filled = wins / settled among filled (status='closed').

- [ ] **Step 1:** Write a failing test: insert positions with statuses pending/open/closed
  (win)/closed (loss)/unfilled for `maker_longshot`; assert `fill_rate` and
  `win_rate_filled` match the hand-computed values.
- [ ] **Step 2:** Run, verify it fails.
- [ ] **Step 3:** Implement `maker_fill_stats` (single grouped query by status +
  realized_pnl sign). `fill_rate = filled/(filled+unfilled)` where filled = open+closed;
  `win_rate_filled = wins/closed` (closed only).
- [ ] **Step 4:** Run green; write `scripts/fill_rate_report.py` that prints it against
  `data/directional.db`.
- [ ] **Step 5:** Commit (`feat(fills): maker fill-rate report`). Commit store.py + script + test.

## Self-Review
- Fill condition (`no_ask <= post_price`) matches a resting NO-buy at post_price.
- `unfilled` status keeps a record for the fill-rate report without polluting P&L/win-rate
  (which query `status='closed'`).
- Live maker path (order_id present) untouched.
