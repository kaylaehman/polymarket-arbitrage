"""
Kalshi WebSocket book maintenance (pure, no socket).

Provides _BookState and the three functions that mutate it:
  apply_snapshot  — reset from a full orderbook_snapshot msg
  apply_delta     — apply a signed cumulative orderbook_delta msg
  book_to_orderbook — convert _BookState to a unified OrderBook

Mirrors the logic in kalshi_client/models.py::KalshiOrderBook.to_unified_orderbook
and kalshi_client/api.py::get_orderbook_unified exactly so the WS path and the
REST path produce identical OrderBook objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


@dataclass
class _BookState:
    """Mutable per-market order book state maintained by WS feed."""

    yes: dict[float, float] = field(default_factory=dict)  # price -> resting size
    no: dict[float, float] = field(default_factory=dict)
    last_seq: Optional[int] = None


def apply_snapshot(state: _BookState, msg: dict) -> None:
    """Reset *state* from an orderbook_snapshot msg dict.

    Prices and sizes in the msg are dollar strings, e.g. "0.6000", "3000.00".
    The caller is responsible for updating state.last_seq from the frame envelope.
    """
    state.yes = {
        float(price): float(size)
        for price, size in msg.get("yes_dollars_fp", [])
    }
    state.no = {
        float(price): float(size)
        for price, size in msg.get("no_dollars_fp", [])
    }


def apply_delta(state: _BookState, msg: dict) -> None:
    """Apply a signed cumulative orderbook_delta msg to *state*.

    delta_fp is the SIGNED cumulative delta to the resting size at price_dollars
    for the given side.  A result <= 0 removes the level entirely.
    """
    side: str = msg["side"]
    price: float = float(msg["price_dollars"])
    delta: float = float(msg["delta_fp"])

    book: dict[float, float] = state.yes if side == "yes" else state.no
    new_size: float = book.get(price, 0.0) + delta
    if new_size <= 0.0:
        book.pop(price, None)
    else:
        book[price] = new_size


def book_to_orderbook(ticker: str, state: _BookState) -> OrderBook:
    """Convert a _BookState to a unified OrderBook.

    Mirrors KalshiOrderBook.to_unified_orderbook exactly:
    - YES bids  = state.yes, sorted DESCENDING (best/highest bid first)
    - YES asks  = derived from NO bids as (1 - no_bid_price), sorted ASCENDING
                  (best/lowest ask first — so OrderBook.best_ask_yes == asks.levels[0])
    - NO bids   = state.no, sorted DESCENDING
    - NO asks   = derived from YES bids as (1 - yes_bid_price), sorted ASCENDING
    """
    yes_token_ob = TokenOrderBook(TokenType.YES)
    no_token_ob = TokenOrderBook(TokenType.NO)

    # YES bids — descending (highest bid first)
    yes_bid_levels = sorted(
        [PriceLevel(price=p, size=s) for p, s in state.yes.items()],
        key=lambda lv: lv.price,
        reverse=True,
    )
    yes_token_ob.bids = OrderBookSide(levels=yes_bid_levels)

    # YES asks — derived from NO bids, ascending (lowest ask first)
    no_bid_items = sorted(state.no.items(), key=lambda kv: kv[0], reverse=True)
    if no_bid_items:
        yes_ask_levels = sorted(
            [PriceLevel(price=1.0 - p, size=s) for p, s in no_bid_items],
            key=lambda lv: lv.price,
        )
        yes_token_ob.asks = OrderBookSide(levels=yes_ask_levels)

    # NO bids — descending
    no_bid_levels = sorted(
        [PriceLevel(price=p, size=s) for p, s in state.no.items()],
        key=lambda lv: lv.price,
        reverse=True,
    )
    no_token_ob.bids = OrderBookSide(levels=no_bid_levels)

    # NO asks — derived from YES bids, ascending
    yes_bid_items = sorted(state.yes.items(), key=lambda kv: kv[0], reverse=True)
    if yes_bid_items:
        no_ask_levels = sorted(
            [PriceLevel(price=1.0 - p, size=s) for p, s in yes_bid_items],
            key=lambda lv: lv.price,
        )
        no_token_ob.asks = OrderBookSide(levels=no_ask_levels)

    return OrderBook(
        market_id=f"kalshi:{ticker}",
        yes=yes_token_ob,
        no=no_token_ob,
        timestamp=datetime.utcnow(),
    )
