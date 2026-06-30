# Blind-spot + hermetic-tests + dashboard — SDD progress
Branch: feature/regional-and-hermetic  Base: main (a58708f)  Executor: inline TDD, paper-safe.

A (2021 blind spot) — DONE, HONEST NEGATIVE RESULT.
  Added catalog_maturity_weight (total/daily ≈ catalog age) + maturity_lambda threaded
  through project_top_artist/backtest. Hypothesis: discount spike-newcomers' forward rate.
  Validated offline on cached 2021/2022/2024 Wayback snapshots @ lam=0/0.5/0.75/1.0:
  did NOT fix 2021 (still Olivia/Kyla/Myke Towers; Bad Bunny's YTD delta is genuinely
  ~1.5B BELOW Olivia at the snapshot — kworb mid-year global deltas disagree with the
  eventual Wrapped #1). 2024 stays 4/4. Root cause: data availability (2021 kworb is
  [Pos,Artist,Total] with NO Daily col; the miss is a YTD gap, not a forward-rate one),
  not a weightable model flaw. => maturity_lambda DEFAULT 0 (off), kept as opt-in knob.
  Also hardened daily fallback: asof_daily.get(a) OR avg-rate (handles 0/absent).

B (full-suite failures) — DONE. 28 failures were NOT network: 27 = asyncio.get_event_loop()
  pollution (fixed -> asyncio.run() in test_polymarket_us _run x2 + test_cross_platform_matcher run);
  1 = test_catalyst time-bomb (catalyst sort used wall-clock datetime.now, calendar dates
  2026-06-21 now in the past). Fixed: scanner gains injectable _now_dt_fn; test pins it to NOW.
  Final full suite: 1051 passed, 0 failed.

C (dashboard) — DONE (data layer; needs container rebuild to show).
  C1 exposure card: top cards read legacy dead arb `portfolio` -> repointed to directional
     per-mode data via renderTopCards().
  C2 paper/actual toggle: store.pnl_summary_by_mode() (TDD, 3 tests) + header Paper/Actual
     toggle; top cards switch bucket. Also fixed strategies list (was recent-50-signals ->
     store.strategies() over all positions; multi_outcome no longer dropped).
