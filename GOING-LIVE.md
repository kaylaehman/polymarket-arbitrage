# Going Live — what's actually required

> Status update (2026-06-17): the execution stubs are now **implemented**.
> Polymarket orders are signed via the official `py-clob-client`; Kalshi orders
> are placed with RSA-PSS request auth. What remains is **credentials + funding**
> and one **architectural gap** (auto-executing both cross-platform legs). Read
> all of this before flipping to live — real money moves once you do.

## What works today (deployed, dry_run)

- Pulls **live** order books for ~5000 Polymarket markets + ~5000 Kalshi markets.
- Real cross-platform + bundle arbitrage **detection** (`core/arb_engine.py`,
  `core/cross_platform_arb.py`).
- Risk manager, portfolio accounting, FastAPI dashboard (`:8888` → host `:8899`).
- In dry_run, fills are simulated (`mode.simulate_fills`, `fill_probability`). No money moves.

## What is now implemented (was stubbed)

- **Polymarket order signing/placement** — `polymarket_client/api.py::place_order`
  live branch builds a signed GTC order via `py-clob-client` (`OrderArgs` →
  `create_order` → `post_order`) using the resolved ERC-1155 token id.
- **Polymarket CLOB auth** — `_init_clob_client()` does L1→L2: uses provided
  api_key/secret/passphrase or derives them from the private key
  (`create_or_derive_api_creds`). Runs on `connect()` in live mode only.
- **Token-id mapping** — `_get_token_id()` resolves YES/NO → on-chain token id
  from the market cache (`clobTokenIds`).
- **Polymarket cancel / positions / open orders / trades** — real CLOB +
  Data-API (`data-api.polymarket.com/positions?user=<addr>`) implementations.
- **Kalshi trading** — `kalshi_client/api.py` now has RSA-PSS request signing
  (`_auth_headers`/`_signed_request`) and `place_order` / `cancel_order` /
  `get_balance` / `get_open_orders` / `get_positions` against `/portfolio/*`.
- Heavy deps (`py-clob-client`, `cryptography`) are **lazy-imported** — dry_run
  never loads them.

Verified 2026-06-17: dry_run order flow works for both clients; the Kalshi
RSA-PSS signature validates against its public key; the ClobClient constructs and
derives the wallet address; all 38 unit tests pass.

## Cross-platform execution (now implemented)

The detect→execute path for Polymarket↔Kalshi arbs is now wired end-to-end:

- **Detection loop** (`run_with_dashboard.py::_monitor_cross_platform_arbs`): after
  market matching completes, it polls order books for the matched pairs (capped by
  `monitoring.cross_platform_max_pairs`, every `monitoring.cross_platform_poll_seconds`),
  runs `CrossPlatformArbEngine.check_arbitrage`, and surfaces hits to the dashboard.
  (Previously `check_arbitrage` was never called — pairs were only matched/displayed.)
- **Atomic executor** (`core/cross_platform_execution.py::CrossPlatformExecutor`):
  executes an opportunity as **two BUYS of complementary outcomes** — buy `token`
  on the cheap venue, buy the *opposite* token on the other venue at `1 - sell_price`.
  This needs no inventory and no shorting, and locks in the same edge the engine
  scored. Both legs are placed **concurrently**; if only one lands it **unwinds**
  the filled leg (sells it back) and logs `[NEEDS-ATTENTION]` if the unwind fails.
- **Caps + gates**: per-trade notional cap (`trading.cross_platform_max_trade_notional`,
  default $15), a per-pair cooldown, and the global risk gate. Order placement is
  hard-gated by `mode.cross_platform_execution_enabled` (default **false**) — even
  in live mode it stays detect-only until you flip it on. dry_run simulates both legs.

Verified: buy/buy translation, notional clamp, cooldown, and all four outcome
paths (both-fill / partial-unwind / both-fail / unwind-fail→exposed).

**Remaining limitation:** placement ≠ confirmed fill. The executor treats a
successful `place_order` as the leg landing; true fill-level reconciliation
(polling each order to confirmed/cancelled and unwinding on a *partial* fill
rather than a *failed placement*) is the next refinement before trusting it with
size. Keep `cross_platform_execution_enabled: false` until you've watched it
detect-only for a while and confirmed the opportunities are real (matcher false
positives exist — see the noisy "sports" matches in logs).

## Kalshi-only mode (US users without Polymarket access)

Polymarket geoblocks US persons, so cross-platform arb usually isn't usable by one
person. For Kalshi-only operation there are two modes (both simulate in dry_run,
place real Kalshi orders only when `trading_mode: live`):

- **`mode.kalshi_native_enabled`** — single-venue **bundle arbitrage on Kalshi**
  (riskless): when a market's `YES_ask + NO_ask < $1` after fees, buy both; one
  side always pays $1. Runs a dedicated `ArbEngine` + `ExecutionEngine` on Kalshi
  order books (`run_with_dashboard.py::_run_kalshi_trading`), watching the top
  `monitoring.kalshi_max_markets` by volume every `kalshi_poll_seconds`. These are
  rare but genuinely free money when they appear.
- **`mode.kalshi_oracle_enabled`** — **directional**, NOT arbitrage: when Kalshi is
  mispriced vs Polymarket, take only the Kalshi leg, using Polymarket as a price
  oracle (`CrossPlatformExecutor.execute_kalshi_leg_only`). Carries real event
  risk — the edge is Kalshi being wrong relative to Polymarket, not a hedge.

Polymarket stays read-only as the reference feed; only your Kalshi creds are used.
Setup: fund Kalshi, create an API key (Settings → API), set `kalshi_api_key_id` +
`kalshi_private_key`, enable a mode, run in dry_run to validate, then `trading_mode: live`.

## Order-size semantics (resolved)

`place_order(size=...)` is **share/contract count**, which is what both the
Polymarket CLOB and Kalshi expect. The dollar→share conversion happens upstream:
the arb engine sizes as `default_order_size / price`, and the cross-platform
engine sizes from order-book liquidity (already shares). `risk_manager.check_order`
caps on `notional = price × size`, so the $-denominated risk limits in `config.yaml`
bind correctly. (Minor pre-existing quirk: `min/max_order_size` act as *share*
floors though their config values read as dollars — harmless small bounds.)

## What still blocks a profitable live run

1. **Credentials + funding (you don't have these yet):**
   - A funded **Polygon wallet** with USDC.e + the **private key**
     (`api.private_key` / env `POLYMARKET_PRIVATE_KEY`).
   - One-time **ERC-20 allowances** for the Polymarket exchange/CTF contracts
     (otherwise the first order is rejected). Easiest: connect the wallet once at
     polymarket.com and approve, or run py-clob-client's approval helper.
   - **CLOB API creds** are auto-derived from the key, so usually nothing to do.
     For an **email/Magic** Polymarket account set `signature_type: 1` (or `2`
     for a browser proxy wallet) **and** `funder` = the USDC-holding address.
   - **Kalshi**: account + an API key pair from Settings → API. Put the UUID in
     `api.kalshi_api_key_id` and the RSA PEM in `api.kalshi_private_key` (or env
     `KALSHI_API_KEY_ID` / `KALSHI_PRIVATE_KEY`; the latter may be a path to a
     `.pem`). Kalshi needs its own funded balance.

2. **Validate the cross-platform signals are real before enabling execution.**
   The executor is built (see above) but `cross_platform_execution_enabled` is
   off by default. The market *matcher* produces false positives (e.g. unrelated
   "sports" pairs), so run detect-only first and confirm the flagged arbs are
   genuinely the same event before flipping execution on. Also note the
   placement-≠-fill limitation above.

## Flipping to live (in order)

1. Provide creds via **env** (preferred) on the compose service, e.g. an
   `.env`/`environment:` block with `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`
   (if proxy), `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`. Never commit secrets;
   `config.live.yaml` is gitignored if you prefer a file.
2. Do the wallet **allowance** approval once.
3. Keep the **tight risk caps** ($50 global / $15 per market / $10 daily loss,
   $15 per cross-platform trade in `config.yaml`). Start even smaller.
4. Set `mode.trading_mode: live` (or add `--live` to the compose `command`) and
   `docker compose up -d`. This trades the **single-platform** (bundle) legs live.
5. Only after watching cross-platform detection and trusting it: set
   `mode.cross_platform_execution_enabled: true` to let the two-leg executor place
   real Kalshi+Polymarket orders.

## Operating the dry_run deployment

```bash
cd ~/docker/polymarket-arbitrage
docker compose up -d --build       # start / rebuild
docker compose logs -f             # watch
docker compose down                # stop
curl -s localhost:8899/api/state         # live order books
curl -s localhost:8899/api/opportunities # detected arbs
```

Dashboard: http://<docker-services>:8899
