"""
Polymarket.US API Client
=========================

Implements BasePolymarketClient against the Polymarket.US REST API.
Uses Ed25519 request signing for authenticated endpoints and httpx for
both authed (api.polymarket.us) and public (gateway.polymarket.us) requests.

Feature-flagged: inert unless mode.polymarket_us_enabled=true in config.yaml.
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import AsyncIterator, Optional

import httpx

from polymarket_client.api import BasePolymarketClient
from polymarket_client.models import (
    Market,
    Order,
    OrderBook,
    OrderBookSide,
    OrderSide,
    OrderStatus,
    Position,
    PriceLevel,
    TokenOrderBook,
    TokenType,
    Trade,
)
from polymarket_us_client.signing import Ed25519Signer

logger = logging.getLogger(__name__)

_BOOK_POLL_INTERVAL = 2.0  # seconds between REST polls in stream_orderbook


class PolymarketUSClient(BasePolymarketClient):
    """
    Polymarket.US REST client implementing BasePolymarketClient.

    Dry-run mode mirrors PolymarketClient: in-memory simulated orders,
    positions, and trades.  Live mode hits api.polymarket.us (authed) and
    gateway.polymarket.us (public).
    """

    def __init__(
        self,
        key_id: str = "",
        secret_key: str = "",
        dry_run: bool = True,
        rest_url: str = "https://api.polymarket.us",
        gateway_url: str = "https://gateway.polymarket.us",
        timeout: float = 30.0,
    ) -> None:
        self.dry_run = dry_run
        self._rest_url = rest_url.rstrip("/")
        self._gateway_url = gateway_url.rstrip("/")
        self._timeout = timeout
        self._signer: Optional[Ed25519Signer] = None
        if key_id and secret_key:
            self._signer = Ed25519Signer(key_id, secret_key)

        # Dry-run state (identical contract to PolymarketClient)
        self._simulated_orders: dict[str, Order] = {}
        self._simulated_positions: dict[str, dict[TokenType, Position]] = {}
        self._simulated_trades: list[Trade] = []

        # Cache: order_id -> market_slug (needed by cancel_order)
        self._order_slug_cache: dict[str, str] = {}

        self._http: Optional[httpx.AsyncClient] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._http = httpx.AsyncClient(timeout=self._timeout)
        mode = "DRY RUN" if self.dry_run else "LIVE"
        logger.info(f"PolymarketUSClient connected ({mode})")

    async def disconnect(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None
        logger.info("PolymarketUSClient disconnected")

    async def __aenter__(self) -> "PolymarketUSClient":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ── HTTP helpers ───────────────────────────────────────────────────────────

    async def _authed_request(self, method: str, path: str, **kw) -> dict:
        """Sign + send a request to the authed API base and return JSON."""
        if self._signer is None:
            raise RuntimeError("No API credentials configured for PolymarketUSClient")
        if self._http is None:
            raise RuntimeError("Client not connected; call connect() first")
        headers = self._signer.auth_headers(method, path)
        url = self._rest_url + path
        resp = await self._http.request(method, url, headers=headers, **kw)
        resp.raise_for_status()
        return resp.json()

    async def _public_request(self, path: str, params: Optional[dict] = None) -> dict:
        """Send an unauthenticated request to the gateway and return JSON."""
        if self._http is None:
            raise RuntimeError("Client not connected; call connect() first")
        url = self._gateway_url + path
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Market data ────────────────────────────────────────────────────────────

    async def list_markets(self, filters: Optional[dict] = None) -> list[Market]:
        params: dict = {"limit": 100, "offset": 0}
        if filters:
            params.update(filters)
        data = await self._public_request("/v1/markets", params)
        markets_raw = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        return [self._parse_market(m) for m in markets_raw]

    async def get_market(self, market_id: str) -> Market:
        data = await self._public_request(f"/v1/market/slug/{market_id}")
        raw = data.get("market", data)
        return self._parse_market(raw)

    def _parse_market(self, raw: dict) -> Market:
        slug = raw.get("slug", raw.get("marketSlug", raw.get("id", "")))
        volume = float(raw.get("volume", raw.get("volume24h", 0)) or 0)
        liquidity = float(raw.get("liquidity", 0) or 0)
        return Market(
            market_id=slug,
            condition_id=slug,
            question=raw.get("question", raw.get("title", "")),
            description=raw.get("description", ""),
            yes_token_id=slug,   # synthetic: slug used as token id
            no_token_id=slug,
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
            volume_24h=volume,
            liquidity=liquidity,
            category=raw.get("category", ""),
        )

    # ── Order book ─────────────────────────────────────────────────────────────

    async def get_orderbook(self, market_id: str) -> OrderBook:
        data = await self._public_request(f"/v1/markets/{market_id}/book")
        return self._parse_orderbook(market_id, data)

    def _parse_orderbook(self, market_id: str, data: dict) -> OrderBook:
        """
        Build YES + synthetic NO books from the raw /book response.

        YES book: bids[] highest-first, offers[] lowest-first.
        NO  book: complement prices, sides reversed.
          NO_bid  = 1 - YES_ask (reversed, so best NO bid = 1 - best YES ask)
          NO_ask  = 1 - YES_bid
        """
        market_data = data.get("marketData", data)
        raw_bids = market_data.get("bids", [])
        raw_asks = market_data.get("offers", market_data.get("asks", []))

        def to_levels(rows: list[dict]) -> list[PriceLevel]:
            out = []
            for r in rows:
                px_obj = r.get("px", {})
                price = float(px_obj.get("value", r.get("price", 0)))
                size = float(r.get("qty", r.get("size", 0)))
                out.append(PriceLevel(price=round(price, 6), size=size))
            return out

        yes_bids = to_levels(raw_bids)
        yes_asks = to_levels(raw_asks)

        # Build synthetic NO book (complement)
        no_bids = [
            PriceLevel(price=round(1 - lvl.price, 6), size=lvl.size)
            for lvl in reversed(yes_asks)   # YES_ask reversed -> NO bid (highest first)
        ]
        no_asks = [
            PriceLevel(price=round(1 - lvl.price, 6), size=lvl.size)
            for lvl in reversed(yes_bids)   # YES_bid reversed -> NO ask (lowest first)
        ]

        yes_book = TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=yes_bids),
            asks=OrderBookSide(levels=yes_asks),
        )
        no_book = TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=no_bids),
            asks=OrderBookSide(levels=no_asks),
        )
        return OrderBook(market_id=market_id, yes=yes_book, no=no_book)

    async def stream_orderbook(
        self,
        market_ids: list[str],
        use_simulation: bool = False,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        """REST-poll each market's order book on an interval."""
        if use_simulation:
            async for item in self._stream_simulated_orderbooks(market_ids):
                yield item
            return

        logger.info(f"[PolymarketUS] Starting orderbook poll for {len(market_ids)} markets")
        try:
            while True:
                for slug in market_ids:
                    try:
                        ob = await self.get_orderbook(slug)
                        yield (slug, ob)
                    except Exception as exc:
                        logger.debug(f"[PolymarketUS] book fetch failed for {slug}: {exc}")
                    await asyncio.sleep(0.05)
                await asyncio.sleep(_BOOK_POLL_INTERVAL)
        except asyncio.CancelledError:
            logger.info("[PolymarketUS] Orderbook stream cancelled")
            raise

    async def _stream_simulated_orderbooks(
        self, market_ids: list[str]
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        import random

        logger.info(f"[PolymarketUS] Starting SIMULATED orderbook stream")
        active = market_ids[:100] if len(market_ids) > 100 else market_ids
        try:
            while True:
                batch = random.sample(active, min(15, len(active)))
                for mid in batch:
                    ob = self._generate_simulated_orderbook(mid)
                    yield (mid, ob)
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise

    def _generate_simulated_orderbook(self, market_id: str) -> OrderBook:
        import random

        yes_mid = 0.50 + random.uniform(-0.30, 0.30)
        inefficiency = (
            random.uniform(-0.08, 0.08) if random.random() < 0.20
            else random.uniform(-0.02, 0.02)
        )
        no_mid = 1.0 - yes_mid + inefficiency
        spread = random.uniform(0.02, 0.06)

        def _levels(mid: float, is_bid: bool, count: int = 5) -> list[PriceLevel]:
            levels = []
            for i in range(count):
                offset = (i + 1) * 0.01
                price = max(0.01, mid - spread / 2 - offset) if is_bid else min(0.99, mid + spread / 2 + offset)
                levels.append(PriceLevel(price=round(price, 2), size=round(random.uniform(100, 1000), 2)))
            return levels

        return OrderBook(
            market_id=market_id,
            yes=TokenOrderBook(
                TokenType.YES,
                bids=OrderBookSide(_levels(yes_mid, True)),
                asks=OrderBookSide(_levels(yes_mid, False)),
            ),
            no=TokenOrderBook(
                TokenType.NO,
                bids=OrderBookSide(_levels(no_mid, True)),
                asks=OrderBookSide(_levels(no_mid, False)),
            ),
        )

    # ── Positions + balance ────────────────────────────────────────────────────

    async def get_positions(self) -> dict[str, dict[TokenType, Position]]:
        if self.dry_run:
            return self._simulated_positions.copy()

        data = await self._authed_request("GET", "/v1/portfolio/positions")
        positions_raw = data.get("positions", data)
        result: dict[str, dict[TokenType, Position]] = {}

        for slug, info in positions_raw.items():
            meta = info.get("marketMetadata", {})
            outcome = meta.get("outcome", "YES").upper()
            token_type = TokenType.YES if outcome == "YES" else TokenType.NO
            size = float(info.get("netPositionDecimal", 0) or 0)
            cost_val = float((info.get("cost") or {}).get("value", 0) or 0)
            realized = float((info.get("realized") or {}).get("value", 0) or 0)
            avg = cost_val / size if size else 0.0

            if slug not in result:
                result[slug] = {}
            result[slug][token_type] = Position(
                market_id=slug,
                token_type=token_type,
                size=size,
                avg_entry_price=avg,
                realized_pnl=realized,
            )
        return result

    async def get_balance(self) -> float:
        if self.dry_run:
            return 10000.0  # simulated balance

        data = await self._authed_request("GET", "/v1/account/balances")
        # Response: {"balances": [{"currentBalance": 50.0, ...}]}
        balances = data.get("balances", [])
        if balances and isinstance(balances, list):
            return float(balances[0].get("currentBalance", 0) or 0)
        return float(data.get("currentBalance", data.get("balance", 0)) or 0)

    # ── Order management ───────────────────────────────────────────────────────

    async def place_order(
        self,
        market_id: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
    ) -> Order:
        order_id = f"pmus_{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            market_id=market_id,
            token_type=token_type,
            side=side,
            price=price,
            size=size,
            status=OrderStatus.OPEN,
            strategy_tag=strategy_tag,
        )

        if self.dry_run:
            logger.info(f"[DRY RUN][PolymarketUS] Placing order: {order}")
            self._simulated_orders[order_id] = order
            return order

        intent, outcome_side, api_price = self._map_order_intent(token_type, side, price)
        body = {
            "marketSlug": market_id,
            "type": "ORDER_TYPE_LIMIT",
            "intent": intent,
            "outcomeSide": outcome_side,
            "price": {"value": str(api_price), "currency": "USD"},
            "quantity": float(size),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        }
        try:
            data = await self._authed_request("POST", "/v1/orders", json=body)
            order.order_id = data.get("orderId", data.get("id", order_id))
            self._order_slug_cache[order.order_id] = market_id
            logger.info(f"[PolymarketUS] Order placed: {order.order_id}")
        except Exception as exc:
            logger.error(f"[PolymarketUS] Failed to place order: {exc}")
            order.status = OrderStatus.REJECTED
            raise
        return order

    @staticmethod
    def _map_order_intent(
        token_type: TokenType, side: OrderSide, price: float
    ) -> tuple[str, str, float]:
        """
        Map (token_type, side, price) to (intent, outcomeSide, api_price).

        YES+BUY  -> BUY_LONG  / OUTCOME_SIDE_YES  / price
        YES+SELL -> SELL_SHORT / OUTCOME_SIDE_YES  / price
        NO+BUY   -> SELL_SHORT / OUTCOME_SIDE_NO   / 1-price  (complement)
        NO+SELL  -> BUY_LONG  / OUTCOME_SIDE_NO   / 1-price
        """
        if token_type == TokenType.YES:
            intent = "ORDER_INTENT_BUY_LONG" if side == OrderSide.BUY else "ORDER_INTENT_SELL_SHORT"
            return intent, "OUTCOME_SIDE_YES", price
        # NO side: complement the price
        api_price = round(1 - price, 6)
        intent = "ORDER_INTENT_SELL_SHORT" if side == OrderSide.BUY else "ORDER_INTENT_BUY_LONG"
        return intent, "OUTCOME_SIDE_NO", api_price

    async def cancel_order(self, order_id: str) -> None:
        if self.dry_run:
            if order_id in self._simulated_orders:
                self._simulated_orders[order_id].status = OrderStatus.CANCELLED
                logger.info(f"[DRY RUN][PolymarketUS] Cancelled order: {order_id}")
            return

        slug = self._order_slug_cache.get(order_id)
        if not slug:
            # Try to look it up
            try:
                raw = await self._authed_request("GET", f"/v1/order/{order_id}")
                slug = raw.get("marketSlug", "")
            except Exception:
                slug = ""

        body = {"marketSlug": slug} if slug else {}
        await self._authed_request("POST", f"/v1/order/{order_id}/cancel", json=body)
        logger.info(f"[PolymarketUS] Cancelled order: {order_id}")

    async def get_order(self, order_id: str) -> dict:
        if self.dry_run:
            o = self._simulated_orders.get(order_id)
            if not o:
                return {"status": OrderStatus.CANCELLED, "filled_size": 0.0, "size": 0.0}
            return {"status": o.status, "filled_size": o.filled_size, "size": o.size}

        try:
            data = await self._authed_request("GET", f"/v1/order/{order_id}")
            size = float(data.get("quantity", data.get("size", 0)) or 0)
            filled = float(data.get("filledQuantity", data.get("filledSize", 0)) or 0)
            st = str(data.get("status", "OPEN")).upper()
            status_map = {
                "OPEN": OrderStatus.OPEN,
                "FILLED": OrderStatus.FILLED,
                "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
                "CANCELLED": OrderStatus.CANCELLED,
                "CANCELED": OrderStatus.CANCELLED,
                "REJECTED": OrderStatus.REJECTED,
            }
            status = status_map.get(st, OrderStatus.OPEN)
            return {"status": status, "filled_size": filled, "size": size}
        except Exception as exc:
            logger.warning(f"[PolymarketUS] get_order({order_id}) failed: {exc}")
            return {"status": OrderStatus.OPEN, "filled_size": 0.0, "size": 0.0}

    async def get_open_orders(self, market_id: Optional[str] = None) -> list[Order]:
        if self.dry_run:
            return [
                o for o in self._simulated_orders.values()
                if o.is_open and (market_id is None or o.market_id == market_id)
            ]

        params = {}
        if market_id:
            params["slugs"] = [market_id]
        try:
            data = await self._authed_request("GET", "/v1/orders/open", params=params)
            raw_orders = data if isinstance(data, list) else data.get("orders", [])
            orders = []
            for item in raw_orders:
                slug = item.get("marketSlug", "")
                oid = item.get("orderId", item.get("id", ""))
                if oid:
                    self._order_slug_cache[oid] = slug
                outcome = str(item.get("outcomeSide", "OUTCOME_SIDE_YES"))
                token_type = TokenType.YES if "YES" in outcome else TokenType.NO
                intent = str(item.get("intent", "ORDER_INTENT_BUY_LONG"))
                side = OrderSide.BUY if "BUY_LONG" in intent else OrderSide.SELL
                price_obj = item.get("price", {})
                price = float(price_obj.get("value", 0) if isinstance(price_obj, dict) else price_obj)
                size = float(item.get("quantity", item.get("size", 0)) or 0)
                filled = float(item.get("filledQuantity", 0) or 0)
                orders.append(Order(
                    order_id=oid,
                    market_id=slug,
                    token_type=token_type,
                    side=side,
                    price=price,
                    size=size,
                    filled_size=filled,
                    status=OrderStatus.OPEN,
                ))
            return orders
        except Exception as exc:
            logger.warning(f"[PolymarketUS] get_open_orders failed: {exc}")
            return []

    async def cancel_all_orders(self, market_id: Optional[str] = None) -> int:
        orders = await self.get_open_orders(market_id)
        cancelled = 0
        for order in orders:
            try:
                await self.cancel_order(order.order_id)
                cancelled += 1
            except Exception as exc:
                logger.warning(f"[PolymarketUS] cancel_all: failed on {order.order_id}: {exc}")
        return cancelled

    async def get_trades(self, market_id: Optional[str] = None, limit: int = 100) -> list[Trade]:
        if self.dry_run:
            trades = self._simulated_trades[-limit:]
            if market_id:
                trades = [t for t in trades if t.market_id == market_id]
            return trades
        # Live: no generic trades endpoint in spec; return empty for now
        return []

    # ── Simulation helpers (identical contract to PolymarketClient) ────────────

    def simulate_fill(self, order_id: str, fill_size: Optional[float] = None) -> Optional[Trade]:
        """Simulate an order fill (dry-run only). SYNC method."""
        if order_id not in self._simulated_orders:
            return None
        order = self._simulated_orders[order_id]
        if not order.is_open:
            return None

        fill_size = fill_size or order.remaining_size
        fill_size = min(fill_size, order.remaining_size)

        fee = fill_size * order.price * 0.015  # 1.5% taker fee
        trade = Trade(
            trade_id=f"trade_{uuid.uuid4().hex[:12]}",
            order_id=order_id,
            market_id=order.market_id,
            token_type=order.token_type,
            side=order.side,
            price=order.price,
            size=fill_size,
            fee=fee,
        )

        order.filled_size += fill_size
        order.updated_at = datetime.utcnow()
        order.status = (
            OrderStatus.FILLED if order.remaining_size <= 0 else OrderStatus.PARTIALLY_FILLED
        )

        self._update_simulated_position(trade)
        self._simulated_trades.append(trade)
        logger.info(f"[DRY RUN][PolymarketUS] Simulated fill: {trade}")
        return trade

    def _update_simulated_position(self, trade: Trade) -> None:
        mid = trade.market_id
        tt = trade.token_type
        if mid not in self._simulated_positions:
            self._simulated_positions[mid] = {}
        if tt not in self._simulated_positions[mid]:
            self._simulated_positions[mid][tt] = Position(
                market_id=mid, token_type=tt, size=0, avg_entry_price=0
            )
        pos = self._simulated_positions[mid][tt]
        if trade.side == OrderSide.BUY:
            new_size = pos.size + trade.size
            if new_size > 0:
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.size + trade.price * trade.size) / new_size
                )
            pos.size = new_size
        else:
            if pos.size > 0:
                pos.realized_pnl += (trade.price - pos.avg_entry_price) * trade.size
            pos.size -= trade.size
