"""
Kalshi WebSocket book maintenance and client.

Provides _BookState and the three pure functions that mutate it:
  apply_snapshot  — reset from a full orderbook_snapshot msg
  apply_delta     — apply a signed cumulative orderbook_delta msg
  book_to_orderbook — convert _BookState to a unified OrderBook

Also provides KalshiWSClient, which connects to the Kalshi orderbook_delta
WS channel, maintains per-ticker _BookState, and fires an async callback on
every valid book update.

The WS path "/trade-api/ws/v2" is passed VERBATIM to _auth_headers — do NOT
prefix it with the REST base URL.  The signing scheme expects exactly this path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

import websockets

from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)

logger = logging.getLogger(__name__)

_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
# _WS_PATH is passed VERBATIM to _auth_headers — do not prefix with base URL
_WS_PATH = "/trade-api/ws/v2"


@dataclass
class _BookState:
    """Mutable per-market order book state maintained by WS feed."""

    yes: dict[float, float] = field(default_factory=dict)  # price -> resting size
    no: dict[float, float] = field(default_factory=dict)


def apply_snapshot(state: _BookState, msg: dict) -> None:
    """Reset *state* from an orderbook_snapshot msg dict.

    Prices and sizes in the msg are dollar strings, e.g. "0.6000", "3000.00".
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


class _SeqGapError(Exception):
    """Raised when a global subscription seq gap is detected.

    Propagates out of _read_loop so the run() reconnect path fires immediately,
    resetting _last_seq and resubscribing to get fresh snapshots for all tickers.
    """


# ── KalshiWSClient ────────────────────────────────────────────────────────────

_OnBookUpdate = Callable[[str, OrderBook], Awaitable[None]]
_ConnectFn = Any  # callable(url, additional_headers=...) -> async context manager
_SleepFn = Callable[[float], Awaitable[None]]

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0


class KalshiWSClient:
    """Event-driven Kalshi orderbook WebSocket client.

    Connects to the Kalshi WS endpoint, subscribes to orderbook_delta for a
    set of tickers, maintains per-ticker _BookState, and calls on_book_update
    after every valid update.

    on_book_update is ALWAYS async — KalshiWSClient always awaits it.  Do not
    pass a sync callable; the call would succeed but the coroutine would be
    silently dropped on the live submit path.
    """

    def __init__(
        self,
        kalshi_client: Any,
        on_book_update: _OnBookUpdate,
        connect_fn: _ConnectFn = websockets.connect,
        sleep_fn: _SleepFn = asyncio.sleep,
    ) -> None:
        self._kalshi = kalshi_client
        self._on_book_update = on_book_update
        self._connect_fn = connect_fn
        self._sleep_fn = sleep_fn

        self.state: str = "disconnected"
        self.last_message_ts: Optional[float] = None
        self.books: dict[str, OrderBook] = {}

        self._states: dict[str, _BookState] = {}
        self._tickers: list[str] = []
        self._stop_event = asyncio.Event()
        self._msg_id: int = 0
        self._last_seq: Optional[int] = None  # global per-subscription seq counter

    # ── public API ──────────────────────────────────────────────────────────

    async def run(self, tickers: list[str]) -> None:
        """Connect, subscribe, and loop until stop() is called."""
        self._tickers = list(tickers)
        self._stop_event.clear()
        backoff = _BACKOFF_BASE

        while not self._stop_event.is_set():
            self.state = "connecting"
            try:
                headers = self._kalshi._auth_headers("GET", _WS_PATH)
                async with self._connect_fn(_WS_URL, additional_headers=headers) as ws:
                    self.state = "connected"
                    backoff = _BACKOFF_BASE
                    self._last_seq = None  # reset on every (re)connect before subscribing
                    await self._subscribe(ws, self._tickers)
                    await self._read_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("KalshiWSClient disconnected: %s — reconnecting in %.0fs", exc, backoff)
                self.state = "reconnecting"
                await self._sleep_fn(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP)

        self.state = "disconnected"

    async def stop(self) -> None:
        """Signal the run loop to exit cleanly."""
        self._stop_event.set()

    def resubscribe(self, tickers: list[str]) -> None:
        """Update the watched set (takes effect on next reconnect only)."""
        self._tickers = list(tickers)

    # ── internals ───────────────────────────────────────────────────────────

    async def _subscribe(self, ws: Any, tickers: list[str]) -> None:
        self._msg_id += 1
        await ws.send(json.dumps({
            "id": self._msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": tickers,
            },
        }))

    async def _read_loop(self, ws: Any) -> None:
        while not self._stop_event.is_set():
            raw = await ws.recv()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("KalshiWSClient: unparseable frame: %s", raw[:200])
                continue
            await self._route_frame(frame)

    async def _route_frame(self, frame: dict) -> None:
        ftype = frame.get("type", "")
        seq = frame.get("seq")

        if ftype == "subscribed":
            return
        if ftype == "error":
            logger.error("KalshiWSClient error frame: %s", frame)
            return

        msg = frame.get("msg", {})
        ticker = msg.get("market_ticker") or msg.get("ticker")
        if not ticker:
            return

        # ── Global seq gap detection ───────────────────────────────────────
        # Kalshi's seq is a single counter PER subscription (per sid), shared
        # across ALL subscribed tickers.  A gap in the global seq means a
        # message was dropped on the wire — we don't know which ticker's book
        # is now stale, so ALL books must be discarded and resynced.
        if seq is not None and self._last_seq is not None:
            if seq != self._last_seq + 1:
                logger.warning(
                    "KalshiWSClient: global seq gap (got %s, expected %s) — "
                    "dropping all books and reconnecting",
                    seq, self._last_seq + 1,
                )
                self.books.clear()
                self._states.clear()
                raise _SeqGapError(f"seq gap: got {seq}, expected {self._last_seq + 1}")

        if seq is not None:
            self._last_seq = seq

        if ftype == "orderbook_snapshot":
            # A snapshot is always a full reset of that ticker's book — valid
            # even mid-stream (without a global-seq gap).
            state = _BookState()
            apply_snapshot(state, msg)
            self._states[ticker] = state
            await self._emit_update(ticker, state)

        elif ftype == "orderbook_delta":
            state = self._states.get(ticker)
            if state is None:
                # No snapshot received yet for this ticker — drop delta.
                logger.debug("KalshiWSClient: delta before snapshot for %s, dropping", ticker)
                return

            apply_delta(state, msg)
            await self._emit_update(ticker, state)

    async def _emit_update(self, ticker: str, state: _BookState) -> None:
        ob = book_to_orderbook(ticker, state)
        self.books[ticker] = ob
        self.last_message_ts = time.monotonic()
        await self._on_book_update(ticker, ob)
