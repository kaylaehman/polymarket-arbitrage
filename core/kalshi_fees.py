"""
Kalshi fee model (canonical)
============================

One source of truth for the Kalshi trading fee so paper P&L, the backtest, and
the multi-outcome detector all agree.

``fee_per_contract(p) = ceil(0.07 * p * (1-p))`` rounded up to the nearest cent.

This mirrors ``backtest/simulate.py`` (the model the +$0.032/trade longshot-NO
result was measured under).  It is deliberately CONSERVATIVE for paper-validation
purposes: Kalshi maker (resting-limit) fills can be cheaper than this on some
series, so charging the full fee understates rather than overstates net EV — the
safe direction for a go-live decision.
"""
from __future__ import annotations

import math


def fee_per_contract(p: float) -> float:
    """Kalshi trading fee for one contract traded at price ``p`` (dollars 0..1)."""
    raw = 0.07 * p * (1.0 - p)
    return math.ceil(raw * 100) / 100.0
