# Catalyst Targeting + Alerts (Sub-project 3) — Design + Plan

**Date:** 2026-06-20  **Status:** approved (autonomous), pre-implementation
**Goal:** (A) ALERTS — push trade/opportunity/position/kill-switch notifications to the user's Discord/Telegram so they see activity without watching the dashboard. (B) CATALYST TARGETING — bias detection toward markets near scheduled catalysts (econ prints, elections, games) where dislocations/edges appear. Gated, never disrupts the live arb.

## Part A — Alerts (primary, high value)
- `core/alerts.py::Alerter` — async, fire-and-forget, NEVER raises into callers (every send wrapped in try/except + short httpx timeout). Sends a formatted message to: Discord webhook (env `ALERT_DISCORD_WEBHOOK`) and/or Telegram (env `ALERT_TELEGRAM_BOT_TOKEN` + `ALERT_TELEGRAM_CHAT_ID`). No-op when unconfigured. Uses httpx (existing dep). Per-(event_type,key) dedup/cooldown (default 60s) to avoid spam.
- `async Alerter.send(event_type: str, title: str, body: str, severity: str = "info", dedup_key: str = "")` — posts to each configured channel. Discord: POST JSON `{"content": "<title>\n<body>"}` (or embed). Telegram: POST `https://api.telegram.org/bot<token>/sendMessage` with `chat_id` + `text`.
- Wire alert hooks (minimal, non-invasive), each calling a shared module-level Alerter only when `alerts.enabled`, failures swallowed:
  - Kalshi-native bundle opportunity found / order submitted (the kalshi sweep + WS detector path).
  - Directional position opened (executor, paper + live).
  - Kill-switch triggered (risk_manager).
  - (optional) WS connection lost/restored.
- Config (`utils/config_loader.py` + `config.yaml`): `alerts.enabled` (default false), `alerts.cooldown_seconds` (60), `alerts.min_severity` ("info"). Secrets via env only (gitignored).
- Gated by `alerts.enabled`; with it off, zero behavior change. One shared Alerter instance on the bot.

## Part B — Catalyst targeting (lighter)
- `core/catalyst.py` — a catalyst CALENDAR (config list of `{name, date (ISO), keywords: [...]}`) + `catalyst_proximity(market_title, market_category, now, calendar, window_hours) -> float` returning a boost in [0,1] when the market title/category matches a catalyst keyword AND the catalyst date is within `window_hours`. Default calendar lives in config (upcoming FOMC/CPI/jobs/election dates — user-editable).
- Use: optionally PRIORITIZE markets in the directional scanner (stable-sort catalyst-near markets first, within the per-cycle cap) and pass a catalyst flag/boost to AI-directional so it preferentially evaluates catalyst-tied markets (where news has an edge). Config: `catalyst.enabled` (default false), `catalyst.window_hours` (72), `catalyst.calendar` (list).
- Honest note: current LIQUID Kalshi markets are long-term (politics/world-events); catalyst-driven markets (FOMC/CPI) are illiquid future-dated series. So Part B mostly READIES the bot for when liquid catalyst markets appear; Part A (alerts) is the immediate value.

## Risk / safety
- Both gated (`alerts.enabled` / `catalyst.enabled`, default false). The Alerter never raises into the live-arb or directional loops. No secrets in git (env only). No change to execution/risk math.

## Testing (TDD, mock httpx — no real network)
- Alerter: builds the correct Discord payload + Telegram URL/params; no-op when unconfigured; dedup/cooldown suppresses a repeat within the window; a send failure (httpx raises) is swallowed and `send` never raises.
- Catalyst: `catalyst_proximity` returns a boost when keyword+window match, 0 otherwise (no keyword match, or catalyst outside window); scanner prioritization orders catalyst-near markets first (stable).
- Wiring: kill-switch / position-opened / opportunity hooks call the alerter (mock) when `alerts.enabled`, and do NOT when disabled.
- ~16-20 tests.

## Out of scope (YAGNI)
OpenClaw-relayed alerts (direct Discord webhook + Telegram is simpler/reliable; the user points them at the same channels OpenClaw uses); auto-fetched catalyst calendar (manual config list for v1); SMS/email; per-channel formatting beyond plain text.
