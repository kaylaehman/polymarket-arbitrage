# Model Improvement + Deferred Items — SDD progress
Branch: feature/model-improvement  Base: main (ab5a341)  Executor: inline TDD (ruflo agents unstable), paper-safe.

- M1 DONE: backtest_year now uses the as-of snapshot's kworb 'Daily' column as the forward
  rate (music_intel/sources/ytd.py:_parse_daily + _parse_floats_after; artist_backtest.py wiring),
  falling back to avg-YTD rate only for pre-Daily snapshots. This matches the LIVE model
  (kworb_artists.parse_artist_rates already reads Daily col 2) so the backtest now honestly
  measures live accuracy. 3 new tests + 6 existing backtest tests green.
  HONEST SWEEP (Daily-rate): 2022 BadBunny 3/3, 2024 Taylor 4/4, 2021 BadBunny 0/3 (Olivia
  Rodrigo/Kyla — model misses Latin-volume weighting), 2023 no Wayback. => 7/10 hit (0.70),
  9/10 top-3 (0.90), up from old 0.56/0.67. Model solid on sustained leaders, weak on 2021.
- M2 DONE: promotion_status gains min_avg_pnl floor (one lucky trade no longer reads "ready")
  + riskless_strategies win-rate exemption (multi_outcome/cross_platform_arb/bundle_arb judged
  on net edge, not hit-rate). build_report threads both. 4 new tests green (10 total).
