"""
Kalshi API Client
=================

Client for interacting with Kalshi prediction market exchange.
Supports public market data endpoints (no authentication required).

API Documentation: https://docs.kalshi.com/getting_started/quick_start_market_data
"""

import asyncio
import base64
import logging
import time
import uuid
from datetime import datetime
from typing import Optional, AsyncIterator
import httpx

from kalshi_client.models import (
    KalshiMarket,
    KalshiOrderBook,
    KalshiEvent,
    KalshiSeries,
)
from polymarket_client.models import (
    PriceLevel,
    OrderBook,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    TokenType,
)

logger = logging.getLogger(__name__)


class KalshiClient:
    """
    Async client for Kalshi prediction market API.
    
    Note: Uses the elections subdomain which provides access to ALL markets,
    not just election-related ones.
    """
    
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    
    def __init__(
        self,
        timeout: float = 30.0,
        max_retries: int = 3,
        dry_run: bool = True,
        api_key_id: str = "",
        private_key_pem: str = "",
    ):
        """
        Initialize Kalshi client.

        Args:
            timeout: Request timeout in seconds
            max_retries: Maximum number of retry attempts
            dry_run: If True, don't place real orders (read-only mode)
            api_key_id: Kalshi API key UUID (required only for trading endpoints)
            private_key_pem: Kalshi RSA private key PEM text (for request signing)
        """
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self.api_key_id = api_key_id or ""
        self._private_key_pem = private_key_pem or ""
        self._private_key = None  # lazily loaded cryptography key object
        self._client: Optional[httpx.AsyncClient] = None
        self._markets_cache: dict[str, KalshiMarket] = {}
        # Simulated state for dry-run trading (mirrors PolymarketClient behaviour)
        self._simulated_orders: dict[str, Order] = {}
        
    async def __aenter__(self) -> "KalshiClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"Accept": "application/json"}
        )
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None
    
    async def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """
        Make a GET request to the Kalshi API.
        
        Args:
            endpoint: API endpoint (without base URL)
            params: Query parameters
            
        Returns:
            JSON response as dictionary
        """
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")
        
        url = f"{self.BASE_URL}{endpoint}"
        
        for attempt in range(self.max_retries):
            try:
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:  # Rate limited
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                    await asyncio.sleep(wait_time)
                elif e.response.status_code == 404:
                    logger.debug(f"Not found: {endpoint}")
                    return {}
                else:
                    logger.error(f"HTTP error {e.response.status_code}: {e}")
                    raise
            except httpx.RequestError as e:
                logger.warning(f"Request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise
        
        return {}

    # =========================================================================
    # AUTHENTICATION (RSA-PSS request signing) + SIGNED REQUESTS
    # =========================================================================

    def _load_private_key(self):
        """Lazily load the RSA private key from PEM. Raises if not configured."""
        if self._private_key is not None:
            return self._private_key
        if not self.api_key_id or not self._private_key_pem:
            raise RuntimeError(
                "Kalshi trading requires api_key_id + private_key (RSA PEM). "
                "Set api.kalshi_api_key_id / api.kalshi_private_key (or env "
                "KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY)."
            )
        # cryptography is only needed for live trading; import lazily.
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        self._private_key = load_pem_private_key(
            self._private_key_pem.encode("utf-8"), password=None
        )
        return self._private_key

    def _auth_headers(self, method: str, path: str) -> dict[str, str]:
        """
        Build Kalshi auth headers for a request.

        Kalshi signs `timestamp(ms) + METHOD + path` with RSA-PSS (SHA-256,
        salt length = digest length). `path` must include the `/trade-api/v2`
        prefix and exclude any query string.
        """
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        key = self._load_private_key()
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
        signature = key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }

    async def _signed_request(
        self,
        method: str,
        endpoint: str,
        json_data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """Make an authenticated request to a Kalshi portfolio endpoint."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        # Signing path = base path (/trade-api/v2) + endpoint, no query string.
        base_path = httpx.URL(self.BASE_URL).path  # "/trade-api/v2"
        sign_path = f"{base_path}{endpoint}"
        url = f"{self.BASE_URL}{endpoint}"

        for attempt in range(self.max_retries):
            try:
                headers = self._auth_headers(method, sign_path)
                response = await self._client.request(
                    method, url, headers=headers, json=json_data, params=params
                )
                response.raise_for_status()
                return response.json() if response.content else {}
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < self.max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                body = e.response.text
                logger.error(f"Kalshi signed {method} {endpoint} -> {e.response.status_code}: {body}")
                raise
            except httpx.RequestError as e:
                logger.warning(f"Kalshi request error (attempt {attempt + 1}): {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(1)
                else:
                    raise
        return {}

    # =========================================================================
    # TRADING ENDPOINTS (authenticated)
    # =========================================================================

    async def place_order(
        self,
        ticker: str,
        token_type: TokenType,
        side: OrderSide,
        price: float,
        size: float,
        strategy_tag: str = "",
        time_in_force: Optional[str] = None,
    ) -> Order:
        """
        Place a limit order on Kalshi.

        Maps the bot's unified order model to Kalshi's POST /portfolio/orders:
          - token_type YES/NO          -> side "yes"/"no"
          - OrderSide BUY/SELL         -> action "buy"/"sell"
          - price (dollars, 0..1)      -> yes_price/no_price in integer cents
          - size                       -> count (whole contracts)

        `ticker` may be the bare Kalshi ticker or the unified "kalshi:<ticker>".
        Returns a unified Order with market_id="kalshi:<ticker>".
        """
        ticker = ticker.split("kalshi:", 1)[-1]
        market_id = f"kalshi:{ticker}"
        order_id = f"korder_{uuid.uuid4().hex[:12]}"

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
            logger.info(f"[DRY RUN] Kalshi place_order: {side.value} {size} {token_type.value} {ticker} @ {price:.2f}")
            self._simulated_orders[order_id] = order
            return order

        price_cents = int(round(price * 100))
        if not 1 <= price_cents <= 99:
            raise ValueError(f"Kalshi price must be 1-99 cents; got {price_cents} (from {price})")

        body = {
            "ticker": ticker,
            "client_order_id": order_id,
            "type": "limit",
            "action": "buy" if side == OrderSide.BUY else "sell",
            "side": "yes" if token_type == TokenType.YES else "no",
            "count": int(round(size)),
        }
        # Kalshi prices each leg in its own field.
        if token_type == TokenType.YES:
            body["yes_price"] = price_cents
        else:
            body["no_price"] = price_cents
        if time_in_force:
            body["time_in_force"] = time_in_force

        try:
            data = await self._signed_request("POST", "/portfolio/orders", json_data=body)
            placed = data.get("order", {})
            order.order_id = placed.get("order_id", order_id)
            status = (placed.get("status") or "").lower()
            order.status = {
                "resting": OrderStatus.OPEN,
                "executed": OrderStatus.FILLED,
                "canceled": OrderStatus.CANCELLED,
                "pending": OrderStatus.PENDING,
            }.get(status, OrderStatus.OPEN)
            logger.info(f"Kalshi order placed: {order.order_id} ({status or 'open'})")
            return order
        except Exception as e:
            logger.error(f"Kalshi place_order failed: {e}")
            order.status = OrderStatus.REJECTED
            raise

    async def cancel_order(self, order_id: str) -> None:
        """Cancel a resting Kalshi order by its order_id."""
        if self.dry_run:
            if order_id in self._simulated_orders:
                self._simulated_orders[order_id].status = OrderStatus.CANCELLED
                logger.info(f"[DRY RUN] Kalshi cancelled order: {order_id}")
            return
        try:
            await self._signed_request("DELETE", f"/portfolio/orders/{order_id}")
            logger.info(f"Kalshi order cancelled: {order_id}")
        except Exception as e:
            logger.error(f"Kalshi cancel_order failed for {order_id}: {e}")
            raise

    async def get_balance(self) -> float:
        """Return the account cash balance in dollars (0.0 in dry-run)."""
        if self.dry_run:
            return 0.0
        data = await self._signed_request("GET", "/portfolio/balance")
        # Kalshi returns balance in cents.
        return float(data.get("balance", 0)) / 100.0

    async def get_open_orders(self, ticker: Optional[str] = None) -> list[Order]:
        """List resting orders, optionally filtered to one ticker."""
        if self.dry_run:
            return [
                o for o in self._simulated_orders.values()
                if o.is_open and (ticker is None or o.market_id == f"kalshi:{ticker.split('kalshi:', 1)[-1]}")
            ]
        params = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker.split("kalshi:", 1)[-1]
        data = await self._signed_request("GET", "/portfolio/orders", params=params)
        orders = []
        for o in data.get("orders", []):
            orders.append(self._parse_order(o))
        return [o for o in orders if o]

    async def get_positions(self) -> dict[str, dict[TokenType, Position]]:
        """
        Return current Kalshi positions keyed by unified market_id then TokenType.
        Kalshi market positions are net YES contracts (negative = net NO).
        """
        if self.dry_run:
            return {}
        data = await self._signed_request("GET", "/portfolio/positions")
        positions: dict[str, dict[TokenType, Position]] = {}
        for p in data.get("market_positions", []):
            ticker = p.get("ticker", "")
            if not ticker:
                continue
            net = int(p.get("position", 0))  # >0 long YES, <0 long NO
            if net == 0:
                continue
            market_id = f"kalshi:{ticker}"
            token_type = TokenType.YES if net > 0 else TokenType.NO
            # market_exposure is in cents across the position; derive avg price.
            exposure_cents = abs(float(p.get("market_exposure", 0)))
            avg_price = (exposure_cents / 100.0 / abs(net)) if net else 0.0
            positions.setdefault(market_id, {})[token_type] = Position(
                market_id=market_id,
                token_type=token_type,
                size=float(abs(net)),
                avg_entry_price=avg_price,
                realized_pnl=float(p.get("realized_pnl", 0)) / 100.0,
            )
        return positions

    def _parse_order(self, o: dict) -> Optional[Order]:
        """Parse a Kalshi order dict into the unified Order model."""
        try:
            side_str = (o.get("side") or "yes").lower()
            action = (o.get("action") or "buy").lower()
            price_cents = o.get("yes_price") if side_str == "yes" else o.get("no_price")
            status = (o.get("status") or "").lower()
            return Order(
                order_id=o.get("order_id", ""),
                market_id=f"kalshi:{o.get('ticker', '')}",
                token_type=TokenType.YES if side_str == "yes" else TokenType.NO,
                side=OrderSide.BUY if action == "buy" else OrderSide.SELL,
                price=float(price_cents or 0) / 100.0,
                size=float(o.get("count", 0)),
                filled_size=float(o.get("count", 0)) - float(o.get("remaining_count", o.get("count", 0))),
                status={
                    "resting": OrderStatus.OPEN,
                    "executed": OrderStatus.FILLED,
                    "canceled": OrderStatus.CANCELLED,
                }.get(status, OrderStatus.OPEN),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Kalshi order: {e}")
            return None

    # =========================================================================
    # SERIES ENDPOINTS
    # =========================================================================
    
    async def get_series(self, series_ticker: str) -> Optional[KalshiSeries]:
        """
        Get information about a series.
        
        Args:
            series_ticker: Series ticker (e.g., "KXHIGHNY")
            
        Returns:
            KalshiSeries object or None if not found
        """
        data = await self._get(f"/series/{series_ticker}")
        if not data or "series" not in data:
            return None
        
        s = data["series"]
        return KalshiSeries(
            ticker=s.get("ticker", series_ticker),
            title=s.get("title", ""),
            frequency=s.get("frequency", ""),
            category=s.get("category", ""),
        )
    
    # =========================================================================
    # EVENTS ENDPOINTS
    # =========================================================================
    
    async def get_event(self, event_ticker: str) -> Optional[KalshiEvent]:
        """
        Get information about an event.
        
        Args:
            event_ticker: Event ticker (e.g., "KXHIGHNY-25DEC08")
            
        Returns:
            KalshiEvent object or None if not found
        """
        data = await self._get(f"/events/{event_ticker}")
        if not data or "event" not in data:
            return None
        
        e = data["event"]
        return KalshiEvent(
            event_ticker=e.get("ticker", event_ticker),
            series_ticker=e.get("series_ticker", ""),
            title=e.get("title", ""),
            category=e.get("category", ""),
        )
    
    # =========================================================================
    # MARKETS ENDPOINTS
    # =========================================================================
    
    async def list_markets(
        self,
        status: str = "open",
        series_ticker: Optional[str] = None,
        event_ticker: Optional[str] = None,
        limit: int = 1000,
        cursor: Optional[str] = None,
    ) -> tuple[list[KalshiMarket], Optional[str]]:
        """
        List markets with optional filters.
        
        Args:
            status: Market status filter (open, closed, settled)
            series_ticker: Filter by series
            event_ticker: Filter by event
            limit: Maximum markets to return (max 1000)
            cursor: Pagination cursor
            
        Returns:
            Tuple of (list of markets, next cursor or None)
        """
        params = {"status": status, "limit": min(limit, 1000)}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        
        data = await self._get("/markets", params=params)
        if not data or "markets" not in data:
            return [], None
        
        markets = []
        for m in data["markets"]:
            market = self._parse_market(m)
            if market:
                markets.append(market)
                self._markets_cache[market.ticker] = market
        
        next_cursor = data.get("cursor")
        return markets, next_cursor
    
    async def list_all_markets(
        self,
        status: str = "open",
        max_markets: int = 10000,
        on_progress: callable = None,  # Callback for progress updates
    ) -> list[KalshiMarket]:
        """
        Fetch all markets with pagination.
        
        Args:
            status: Market status filter
            max_markets: Maximum total markets to fetch
            on_progress: Optional callback(loaded_count) for progress updates
            
        Returns:
            List of all markets
        """
        all_markets = []
        cursor = None
        
        while len(all_markets) < max_markets:
            markets, next_cursor = await self.list_markets(
                status=status,
                limit=1000,
                cursor=cursor,
            )
            
            if not markets:
                break
            
            all_markets.extend(markets)
            logger.info(f"Kalshi: {len(all_markets)} markets loaded...")
            
            # Report progress
            if on_progress:
                try:
                    on_progress(len(all_markets))
                except:
                    pass
            
            if not next_cursor:
                break
            cursor = next_cursor
            
            # Small delay to avoid rate limiting
            await asyncio.sleep(0.2)
        
        logger.info(f"Kalshi: {len(all_markets)} total markets loaded ✓")
        return all_markets[:max_markets]
    
    async def get_market(self, ticker: str) -> Optional[KalshiMarket]:
        """
        Get a specific market by ticker.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiMarket object or None if not found
        """
        # Check cache first
        if ticker in self._markets_cache:
            return self._markets_cache[ticker]
        
        data = await self._get(f"/markets/{ticker}")
        if not data or "market" not in data:
            return None
        
        market = self._parse_market(data["market"])
        if market:
            self._markets_cache[ticker] = market
        return market
    
    def _parse_market(self, data: dict) -> Optional[KalshiMarket]:
        """Parse market data from API response."""
        try:
            # Prices come in cents, convert to dollars
            yes_price = data.get("yes_price", 0) / 100.0 if data.get("yes_price") else 0.0
            no_price = data.get("no_price", 0) / 100.0 if data.get("no_price") else 0.0
            
            # If no_price not given, derive from yes_price
            if no_price == 0 and yes_price > 0:
                no_price = 1.0 - yes_price
            
            # Parse close time
            close_time = None
            if data.get("close_time"):
                try:
                    close_time = datetime.fromisoformat(data["close_time"].replace("Z", "+00:00"))
                except:
                    pass
            
            return KalshiMarket(
                ticker=data.get("ticker", ""),
                event_ticker=data.get("event_ticker", ""),
                series_ticker=data.get("series_ticker", ""),
                title=data.get("title", ""),
                subtitle=data.get("subtitle", ""),
                yes_price=yes_price,
                no_price=no_price,
                status=data.get("status", ""),
                result=data.get("result"),
                volume=data.get("volume", 0),
                open_interest=data.get("open_interest", 0),
                close_time=close_time,
                category=data.get("category", ""),
            )
        except Exception as e:
            logger.warning(f"Failed to parse Kalshi market: {e}")
            return None
    
    # =========================================================================
    # ORDERBOOK ENDPOINTS
    # =========================================================================
    
    async def get_orderbook(self, ticker: str) -> Optional[KalshiOrderBook]:
        """
        Get order book for a market.
        
        Args:
            ticker: Market ticker
            
        Returns:
            KalshiOrderBook object or None if not found
        """
        data = await self._get(f"/markets/{ticker}/orderbook")
        if not data or "orderbook" not in data:
            return None
        
        ob = data["orderbook"]
        
        # Parse YES bids (prices in cents)
        yes_bids = []
        for level in ob.get("yes", []):
            if len(level) >= 2:
                price_cents = level[0]
                quantity = level[1]
                yes_bids.append(PriceLevel(
                    price=price_cents / 100.0,  # Convert to dollars
                    size=float(quantity)
                ))
        
        # Parse NO bids (prices in cents)
        no_bids = []
        for level in ob.get("no", []):
            if len(level) >= 2:
                price_cents = level[0]
                quantity = level[1]
                no_bids.append(PriceLevel(
                    price=price_cents / 100.0,
                    size=float(quantity)
                ))
        
        # Sort bids descending (best/highest first)
        yes_bids.sort(key=lambda x: x.price, reverse=True)
        no_bids.sort(key=lambda x: x.price, reverse=True)
        
        return KalshiOrderBook(
            ticker=ticker,
            yes_bids=yes_bids,
            no_bids=no_bids,
            timestamp=datetime.utcnow(),
        )
    
    async def get_orderbook_unified(self, ticker: str) -> Optional[OrderBook]:
        """
        Get order book in unified format (compatible with Polymarket).
        
        Args:
            ticker: Market ticker
            
        Returns:
            OrderBook object or None if not found
        """
        kalshi_ob = await self.get_orderbook(ticker)
        if not kalshi_ob:
            return None
        return kalshi_ob.to_unified_orderbook()
    
    # =========================================================================
    # STREAMING (Polling-based for public API)
    # =========================================================================
    
    async def stream_orderbooks(
        self,
        tickers: list[str],
        batch_size: int = 100,
        rotation_delay: float = 2.0,
    ) -> AsyncIterator[tuple[str, OrderBook]]:
        """
        Stream order books for multiple markets using polling.
        
        Args:
            tickers: List of market tickers to stream
            batch_size: Number of markets to fetch per batch
            rotation_delay: Delay between batches in seconds
            
        Yields:
            Tuple of (ticker, OrderBook) for each update
        """
        logger.info(f"Starting Kalshi orderbook stream for {len(tickers)} markets")
        
        while True:
            for i in range(0, len(tickers), batch_size):
                batch = tickers[i:i + batch_size]
                logger.debug(f"Fetching Kalshi orderbooks {i+1}-{min(i+batch_size, len(tickers))} of {len(tickers)}")
                
                # Fetch orderbooks in parallel
                tasks = [self.get_orderbook_unified(ticker) for ticker in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for ticker, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.debug(f"Failed to get Kalshi orderbook for {ticker}: {result}")
                        continue
                    if result:
                        yield (ticker, result)
                
                await asyncio.sleep(rotation_delay)
    
    # =========================================================================
    # CATEGORY/SEARCH HELPERS
    # =========================================================================
    
    async def get_markets_by_category(self, category: str) -> list[KalshiMarket]:
        """
        Get all open markets in a category.
        
        Common categories: elections, economics, crypto, tech, entertainment
        """
        # Kalshi API doesn't have a direct category filter, so we fetch all
        # and filter client-side
        all_markets = await self.list_all_markets(status="open")
        return [m for m in all_markets if m.category.lower() == category.lower()]
    
    async def search_markets(self, query: str) -> list[KalshiMarket]:
        """
        Search markets by title.
        
        Args:
            query: Search query string
            
        Returns:
            List of matching markets
        """
        all_markets = await self.list_all_markets(status="open")
        query_lower = query.lower()
        return [
            m for m in all_markets 
            if query_lower in m.title.lower() or query_lower in m.subtitle.lower()
        ]

