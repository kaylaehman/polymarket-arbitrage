"""
Multi-Outcome Mutually-Exclusive Kalshi Arbitrage (riskless)
============================================================

A Kalshi *event* whose markets are flagged ``mutually_exclusive`` (e.g. the
temperature-bucket events ``KXHIGHNY-26JUN25`` with buckets B79.5 / B81.5 / …
plus the threshold tail) partitions the outcome space: **exactly one** market
resolves YES (pays $1) and every other resolves NO (pays $0).

Therefore buying **1 YES contract on every market in the event** costs
``sum(yes_ask_i)`` and is *guaranteed* to pay back exactly $1 — independent of
which outcome occurs.  When ::

    sum(yes_ask_i) + sum(fees_i) < $1

that is a **riskless** profit (an "underround").  This is the multi-outcome
generalisation of the 2-way ``YES_ask + NO_ask < $1`` bundle arb, and it
mis-prices more often because there are more legs to drift out of line.

Safety
------
This module is **detection only** and is deliberately pure / I/O-free.  Risklessness
depends on actually completing the *whole* cover, so the detector refuses to
report an opportunity unless **every** leg has a usable ask price *and* a
resting ask size to fill against — a partially-filled cover is NOT riskless and
would be a real directional loss.  Live multi-leg placement (which additionally
needs all-or-nothing fill handling) is intentionally out of scope here.

The fee model mirrors ``backtest/simulate.py``: ``ceil(0.07 * p * (1-p))`` per
contract, rounded up to the nearest cent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# Canonical Kalshi fee model (shared with the tracker + backtest). Re-exported
# here so existing callers/tests of this module keep working.
from core.kalshi_fees import fee_per_contract  # noqa: F401


@dataclass(frozen=True)
class OutcomeLeg:
    """One market within a mutually-exclusive event.

    Ask prices/sizes come from the market's live orderbook (the Kalshi list
    endpoints return None for these — only the orderbook has them).  A leg with
    no usable ask on a given side cannot be filled on that side and therefore
    blocks the (otherwise riskless) cover for that side.  ``no_ask`` defaults to
    None so existing YES-only callers keep working.
    """
    ticker: str
    yes_ask: Optional[float]
    yes_ask_size: Optional[int]
    no_ask: Optional[float] = None
    no_ask_size: Optional[int] = None


@dataclass(frozen=True)
class MultiOutcomeArb:
    """A detected riskless multi-outcome lock.

    Two duals, both riskless on a mutually-exclusive + exhaustive event:
      * ``side="YES"`` — buy 1 YES on every leg; exactly one pays $1
        (``payout_per_cover == 1``).  Locks when ``Σ yes_ask + fees < 1``.
      * ``side="NO"``  — buy 1 NO on every leg; exactly N-1 pay $1
        (``payout_per_cover == N-1``).  Locks when ``Σ no_ask + fees < N-1``.

    Field names keep the original (YES) shape for back-compat; for the NO side
    they read "per cover" (one contract bought on each leg).
    """
    event_ticker: str
    legs: tuple[tuple[str, float], ...]   # (ticker, side_ask) per leg
    contracts: int                        # contracts to buy on EACH leg
    cost_per_contract: float              # Σ side_ask across legs
    fees_per_contract: float              # Σ per-leg fees
    net_edge_per_contract: float          # payout_per_cover - cost - fees
    total_profit: float                   # net_edge_per_contract * contracts
    side: str = "YES"                     # "YES" | "NO"
    payout_per_cover: float = 1.0         # 1 for YES, N-1 for NO


def _detect_side(
    event_ticker: str,
    legs: Sequence[OutcomeLeg],
    *,
    side: str,
    payout_per_cover: float,
    min_edge: float,
    max_contracts: int,
) -> Optional[MultiOutcomeArb]:
    """Detect a riskless cover on one side (YES or NO). See callers below."""
    cost = 0.0
    fees = 0.0
    sizes: list[int] = []
    leg_pairs: list[tuple[str, float]] = []
    for leg in legs:
        if side == "YES":
            price, size = leg.yes_ask, leg.yes_ask_size
        else:
            price, size = leg.no_ask, leg.no_ask_size
        if price is None or not (0.0 < price < 1.0):
            return None  # can't complete the cover → not riskless
        if size is None or size < 1:
            return None  # can't size/fill this leg → not riskless
        cost += price
        fees += fee_per_contract(price)
        sizes.append(int(size))
        leg_pairs.append((leg.ticker, price))

    net_edge = payout_per_cover - cost - fees
    if net_edge < min_edge:
        return None

    contracts = min(min(sizes), max_contracts)
    if contracts < 1:
        return None

    return MultiOutcomeArb(
        event_ticker=event_ticker,
        legs=tuple(leg_pairs),
        contracts=contracts,
        cost_per_contract=round(cost, 4),
        fees_per_contract=round(fees, 4),
        net_edge_per_contract=round(net_edge, 4),
        total_profit=round(net_edge * contracts, 4),
        side=side,
        payout_per_cover=round(payout_per_cover, 4),
    )


def detect_multi_outcome_arb(
    event_ticker: str,
    legs: Sequence[OutcomeLeg],
    *,
    min_edge: float = 0.01,
    max_contracts: int = 10,
) -> Optional[MultiOutcomeArb]:
    """Detect a riskless multi-outcome lock across a mutually-exclusive event.

    Checks both duals and returns the first that locks (YES preferred — it
    deploys less capital), else ``None``:
      * YES underround: buy 1 YES on every leg, payout $1.
      * NO  overround:  buy 1 NO  on every leg, payout (N-1).

    The caller MUST only pass legs from an event Kalshi marks
    ``mutually_exclusive`` and collectively exhaustive (so exactly one leg
    resolves YES) — this function assumes that and does not re-derive it.

    Guards (any failure on a side → that side doesn't lock): fewer than 2 legs;
    any leg missing a usable ask price on that side; any leg missing a resting
    ask size on that side; net edge after fees below ``min_edge``.

    Sizing: ``contracts`` = min resting ask size across legs, capped at
    ``max_contracts``.
    """
    if len(legs) < 2:
        return None
    n = len(legs)
    yes = _detect_side(event_ticker, legs, side="YES", payout_per_cover=1.0,
                       min_edge=min_edge, max_contracts=max_contracts)
    if yes is not None:
        return yes
    return _detect_side(event_ticker, legs, side="NO", payout_per_cover=float(n - 1),
                        min_edge=min_edge, max_contracts=max_contracts)
