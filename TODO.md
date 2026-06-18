# TODO

Working notes for the intelligence-layer work and what's left. Status as of the
`intelligence-layer` branch (PR #4), merged up to date with `main`'s live-execution work.

---

## ✅ Done (this line of work)

- [x] **Phase 1** — `intelligence/` scaffold (signal, cache, topic extractor, news fetcher, AI analyzer, engine)
- [x] **Phase 2** — core hooks, annotate-only (`Opportunity.signal`, config, entry-point wiring)
- [x] **FEAT-02** — resolution-criteria awareness in the Claude prompt
- [x] **FEAT-05** — Kelly sizing (`core/kelly.py`) + execution hook (disabled by default)
- [x] **FEAT-07** — time-decay edge discounting + hard-skip near resolution
- [x] **FEAT-09** — signal DB (SQLite) + hourly outcome poller + `get_signal_accuracy`
- [x] **Phase 3** — dashboard "AI Signals" panel + `/health`
- [x] **Cross-platform monitor** — live detect + AI annotation (detect/flag only)
- [x] **OpenClaw control API** — `/api/agent` read + control, token-gated, disabled by default

---

## 🔑 Blocked on you (credentials / decisions)

- [ ] Set env vars to actually run the AI layer: `NEWSAPI_KEY`, `ANTHROPIC_API_KEY`
      (or `ANTHROPIC_BASE_URL` → OpenClaw proxy)
- [ ] Set `AGENT_API_TOKEN` (+ `agent.enabled: true`, `agent.allow_control: true`)
      before the OpenClaw agent can reach `/api/agent`
- [ ] Fix git author name: commits show `Kayla`, your rules say `kaylaehman`
      (`git config` change — yours to make)
- [ ] Decide review/merge of PR #4

---

## 🚀 Enablement path (to make the AI layer actually pay off)

The DB starts empty; accuracy/Kelly need real history. Order matters:

1. [ ] Enable `intelligence.enabled` + `database.enabled` in `config.yaml`, run live (dry_run is fine)
2. [ ] Let it accumulate signals while markets resolve (the outcome poller fills `outcomes`)
3. [ ] After ~2 weeks, check `SignalDB.get_signal_accuracy()` — confirm `calibration_error` is low
4. [ ] **Only then** consider `trading.kelly_enabled: true` (miscalibrated + Kelly = ruin)

---

## 🔌 Follow-up wiring

- [ ] **Connect the cross-platform monitor to live execution.** `main` now has
      `core/cross_platform_execution.py`; the monitor currently only detects + annotates.
      Wire `should_filter`/confirmed opportunities into the executor (gated by
      `mode.cross_platform_execution_enabled`).
- [ ] **Reconsider Kelly placement.** It hooks the bundle/MM path (riskless, directionless).
      Now that directional cross-platform execution exists, Kelly belongs there.
- [ ] **DB logging is coupled to the intelligence path** — opportunities are only logged
      when intelligence is enabled. Optionally log opportunities unconditionally.
- [ ] **Verify `_parse_resolution`** against a real Gamma resolved-market payload before
      trusting accuracy numbers (best-effort; returns None/skip if the shape differs).

---

## ✅ Verification gaps (not runnable in dev — no live network)

- [ ] Full `run_with_dashboard.py` boot in simulation, then real-data dry_run
- [ ] Cross-platform monitor end-to-end (Kalshi orderbook fetch + matching live)
- [ ] Outcome poller against live Gamma resolutions
- [ ] `/api/agent` against the actual OpenClaw agent (auth + control round-trip)

---

## 📋 Remaining FEATURES backlog

- [ ] **FEAT-01** Whale wallet tracker — needs a Dune API key + a starter list of wallet addresses
- [ ] **FEAT-03** Cross-platform crowd-disagreement signal (partly covered by the monitor)
- [ ] **FEAT-04** Reddit / X news sources (Reddit needs no key)
- [ ] **FEAT-06** Correlated-market exposure limits (Claude-classified themes)
- [ ] **FEAT-08** Telegram notifications — needs `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`
      (outbound only as specced; interactive/agent version is a separate build)
- [ ] **FEAT-10** Backtester extension — **prereq: FEAT-09 has ≥2 weeks of signal data**
- [x] **FEAT-11** Docker deploy — Dockerfile + docker-compose + `.env` came in via `main`;
      `/health` endpoint added. (Verify the compose stack on the homelab.)

---

## 🧹 Housekeeping / tech debt

- [ ] `datetime.utcnow()` deprecation warnings throughout (pre-existing; codebase-wide)
- [ ] Consider trimming the 86KB embedded dashboard HTML into `templates/` + `static/`
- [ ] No tests yet for `main`'s live-execution modules (`cross_platform_execution.py`,
      Kalshi-native modes) — add characterization tests before trusting live mode
