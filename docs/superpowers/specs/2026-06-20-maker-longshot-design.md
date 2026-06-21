# Maker (Longshot/NO-bias) Strategy (Sub-project 2) — Design + Plan

**Date:** 2026-06-20  **Status:** approved (autonomous), pre-implementation
**Goal:** Capture the documented Kalshi longshot bias (NO beats YES on longshots) + the maker edge (0% maker fee vs 1.5% taker) by POSTING resting NO BUY limit orders on structurally-favored longshot Kalshi markets. A third strategy inside the existing directional engine. PAPER-FIRST, gated, separate caps.

## Decisions (autonomous, grounded in Jon-Becker/pma research)
- Venue: Kalshi. Fits as strategy #3 in core/directional/ alongside Safe Compounder + AI-directional. Reuses: the liquid-market scanner, utils/structural_bias.structural_score, the decider/Kelly+caps, the store, the dashboard panel, the directional risk caps + kill switch.
- Edge: on longshot markets (YES mid <= max_yes_price, default 0.15 -> NO is heavy favorite ~0.85+), the structural longshot/NO bias makes NO underpriced. Acting as MAKER (resting NO BUY limit, 0% fee) captures the spread + the bias. Hold to resolution (favorite usually wins -> NO resolves YES -> profit). Variance-bearing: a longshot occasionally hits and NO loses the position; +EV is in aggregate, hence PAPER-FIRST.

## Components (reuse-heavy)
- core/directional/strategies/maker_longshot.py::MakerLongshotStrategy(min_structural_score, max_yes_price, price_improvement_cents, skip_categories): scan(markets, ctx) -> candidates. For each market: yes_mid = market.yes_price (set by scanner from the book); if yes_mid > max_yes_price skip; score = structural_score(1 - yes_mid, "NO", category) — passes the NO-side price so the bias lookup reads the correct longshot direction; if score < min skip. Build a NO candidate priced as a MAKER resting bid: no_bid = ctx no-bid for the ticker (or 1 - yes_ask); post_price = min(no_bid + price_improvement_cents/100, max_no_price) — a non-marketable resting NO BUY (rests in the book, maker). strategy="maker_longshot".
- Config (utils/config_loader.py DirectionalConfig + config.yaml): directional.maker_longshot: { mode: paper, min_structural_score: 0.02, max_yes_price: 0.15, price_improvement_cents: 1, order_ttl_minutes: 60, skip_categories: [] }. Uses the shared directional caps (0//4).
- Executor: place a RESTING (non-marketable) NO BUY limit. paper: record the position at post_price (simulate maker fill — conservative edge proxy) immediately. live: kalshi place_order(NO, BUY, price=post_price, size, time_in_force resting/GTC) -> record a PENDING maker position with the order_id; do not assume fill.
- Tracker (extend): for live maker positions in PENDING state: poll get_order(order_id); on fill -> mark OPEN (hold to resolution); if age > order_ttl_minutes and unfilled -> cancel_order + drop. Resolution settle (existing) books P&L. Safe Compounder-style hold-to-resolution (no stop-loss). paper positions are OPEN immediately.
- Engine: wire MakerLongshotStrategy as the third strategy (per its config.mode).
- Dashboard: maker positions/signals appear in the existing /api/directional panel (strategy="maker_longshot").

## Risk / safety
- Gated by directional.enabled AND maker_longshot.mode (paper default). Live requires explicit mode flip after paper validation (positive expectancy over N resolved bets). Shared directional caps + global kill switch apply. Never exceeds /position, 0 total, 4 open. Paper books P&L GROSS of fees (noted).

## Testing (TDD, mock client + intelligence not needed here)
- Strategy: emits NO maker candidate on a longshot with structural edge; skips when yes_mid > max_yes_price; skips when score < min; post_price is a non-marketable resting bid (<= no_ask). 
- Executor: paper records position at post_price (no API); live places a resting limit (mock) + records PENDING with order_id.
- Tracker: pending->fill transitions to OPEN; pending past TTL -> cancel; resolution settle books P&L sign correct.
- Config defaults; engine wires the third strategy.
- ~14-18 tests. No live API in tests.

## Out of scope (YAGNI)
Re-posting/laddering unfilled orders (v1 cancels on TTL); dynamic re-pricing; PM.US maker.
