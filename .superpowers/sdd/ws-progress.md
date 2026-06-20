# WebSocket feeds — SDD ledger
Branch: feature/websocket-feeds
Base: 82603d8

Step 0 CONFIRMED live WS format (2026-06-20):
- Frame: {"type","sid","seq",msg:{...}}; seq is TOP-LEVEL. types: subscribed(ack/ignore), orderbook_snapshot, orderbook_delta, error.
- snapshot msg: {market_ticker, yes_dollars_fp:[[price_str,size_str]...], no_dollars_fp:[...]} (DOLLAR strings)
- delta msg: {market_ticker, price_dollars:str, delta_fp:signed-str (cumulative delta to resting size), side:"yes"|"no", ts_ms}
- apply_snapshot: reset {float(p):float(s)} from *_dollars_fp. apply_delta: state[side][float(price_dollars)] += float(delta_fp); drop if <=0.
Task 1-2: complete (b07da09..8c0f015, 14 tests)
Task 3-5: complete (d749f8d..695beac, 27 tests; seq-gap+async+reason confirmed)
Task 6-7: complete (a18d875..5d3c19d). WS tests 29 pass; full-suite 27 fails are pre-existing (polymarket_us isolation). Ready for final review.
