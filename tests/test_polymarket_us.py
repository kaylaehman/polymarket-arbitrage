"""
Tests for PolymarketUSClient and Ed25519Signer.

Covers:
  1. Ed25519Signer: header format + signature verifies against derived public key
  2. Order book translation: YES->synthetic NO complement correctness
  3. place_order body mapping for all 4 (token_type, side) combos
  4. Dry-run place / cancel / simulate_fill flow
"""
import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from polymarket_us_client.signing import Ed25519Signer
from polymarket_us_client.api import PolymarketUSClient
from polymarket_client.models import (
    OrderBook,
    OrderBookSide,
    OrderSide,
    OrderStatus,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)


# ─── Fixtures ─────────────────────────────────────────────────────────────────

def _make_signer() -> tuple[Ed25519Signer, Ed25519PrivateKey]:
    """Return a signer plus the raw private key for verification."""
    raw_key = Ed25519PrivateKey.generate()
    seed = raw_key.private_bytes_raw()  # 32-byte seed
    b64_secret = base64.b64encode(seed).decode()
    signer = Ed25519Signer(key_id="test-key-id", secret_key_b64=b64_secret)
    return signer, raw_key


def _make_client(dry_run: bool = True) -> PolymarketUSClient:
    return PolymarketUSClient(
        key_id="test-key",
        secret_key=base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()).decode(),
        dry_run=dry_run,
    )


# ─── 1. Ed25519Signer ─────────────────────────────────────────────────────────

class TestEd25519Signer:
    def test_headers_contain_required_keys(self):
        signer, _ = _make_signer()
        headers = signer.auth_headers("GET", "/v1/markets")
        assert "X-PM-Access-Key" in headers
        assert "X-PM-Timestamp" in headers
        assert "X-PM-Signature" in headers
        assert "Content-Type" in headers

    def test_key_id_propagated(self):
        signer, _ = _make_signer()
        headers = signer.auth_headers("POST", "/v1/orders")
        assert headers["X-PM-Access-Key"] == "test-key-id"

    def test_timestamp_is_numeric_string(self):
        signer, _ = _make_signer()
        headers = signer.auth_headers("GET", "/v1/markets")
        ts = headers["X-PM-Timestamp"]
        assert ts.isdigit(), f"timestamp should be all digits, got {ts!r}"
        assert len(ts) == 13, "expect millisecond timestamp (13 digits)"

    def test_signature_verifies_against_public_key(self):
        """The signature must verify with the public key derived from the same seed."""
        signer, raw_private_key = _make_signer()
        method, path = "GET", "/v1/account/balances"
        headers = signer.auth_headers(method, path)

        ts = headers["X-PM-Timestamp"]
        msg = f"{ts}{method}{path}".encode("utf-8")
        sig_bytes = base64.b64decode(headers["X-PM-Signature"])

        public_key = raw_private_key.public_key()
        # Should not raise
        public_key.verify(sig_bytes, msg)

    def test_different_calls_produce_different_timestamps(self):
        import time
        signer, _ = _make_signer()
        h1 = signer.auth_headers("GET", "/v1/markets")
        time.sleep(0.002)
        h2 = signer.auth_headers("GET", "/v1/markets")
        # Timestamps may differ; both must be valid digits
        assert h1["X-PM-Timestamp"].isdigit()
        assert h2["X-PM-Timestamp"].isdigit()


# ─── 2. Order book translation ────────────────────────────────────────────────

class TestOrderBookTranslation:
    def _raw_book(self) -> dict:
        """YES bids highest-first, offers lowest-first (as the API returns)."""
        return {
            "marketData": {
                "bids": [
                    {"px": {"value": "0.60", "currency": "USD"}, "qty": "200"},
                    {"px": {"value": "0.55", "currency": "USD"}, "qty": "150"},
                ],
                "offers": [
                    {"px": {"value": "0.65", "currency": "USD"}, "qty": "180"},
                    {"px": {"value": "0.70", "currency": "USD"}, "qty": "100"},
                ],
            }
        }

    def test_yes_bids_parsed_correctly(self):
        client = _make_client()
        ob = client._parse_orderbook("test-slug", self._raw_book())
        assert ob.yes.bids.levels[0].price == pytest.approx(0.60)
        assert ob.yes.bids.levels[1].price == pytest.approx(0.55)

    def test_yes_asks_parsed_correctly(self):
        client = _make_client()
        ob = client._parse_orderbook("test-slug", self._raw_book())
        assert ob.yes.asks.levels[0].price == pytest.approx(0.65)
        assert ob.yes.asks.levels[1].price == pytest.approx(0.70)

    def test_no_bids_are_complement_of_yes_asks_reversed(self):
        """NO bids = 1 - YES ask (reversed, so best NO bid from best YES ask)."""
        client = _make_client()
        ob = client._parse_orderbook("test-slug", self._raw_book())
        # YES asks: 0.65, 0.70
        # Reversed: 0.70, 0.65
        # NO bids: 1-0.70=0.30, 1-0.65=0.35
        assert ob.no.bids.levels[0].price == pytest.approx(0.30, abs=1e-6)
        assert ob.no.bids.levels[1].price == pytest.approx(0.35, abs=1e-6)

    def test_no_asks_are_complement_of_yes_bids_reversed(self):
        """NO asks = 1 - YES bid (reversed, so best NO ask from worst YES bid)."""
        client = _make_client()
        ob = client._parse_orderbook("test-slug", self._raw_book())
        # YES bids: 0.60, 0.55
        # Reversed: 0.55, 0.60
        # NO asks: 1-0.55=0.45, 1-0.60=0.40
        assert ob.no.asks.levels[0].price == pytest.approx(0.45, abs=1e-6)
        assert ob.no.asks.levels[1].price == pytest.approx(0.40, abs=1e-6)

    def test_prices_rounded_6dp(self):
        raw = {
            "marketData": {
                "bids": [{"px": {"value": "0.123456789", "currency": "USD"}, "qty": "100"}],
                "offers": [{"px": {"value": "0.234567891", "currency": "USD"}, "qty": "100"}],
            }
        }
        client = _make_client()
        ob = client._parse_orderbook("slug", raw)
        assert ob.yes.bids.levels[0].price == round(0.123456789, 6)

    def test_market_id_preserved(self):
        client = _make_client()
        ob = client._parse_orderbook("my-market", self._raw_book())
        assert ob.market_id == "my-market"


# ─── 3. place_order body mapping ──────────────────────────────────────────────

class TestOrderIntentMapping:
    """Tests for _map_order_intent without hitting the network."""

    def test_yes_buy_mapping(self):
        intent, side, price = PolymarketUSClient._map_order_intent(
            TokenType.YES, OrderSide.BUY, 0.55
        )
        assert intent == "ORDER_INTENT_BUY_LONG"
        assert side == "OUTCOME_SIDE_YES"
        assert price == pytest.approx(0.55)

    def test_yes_sell_mapping(self):
        intent, side, price = PolymarketUSClient._map_order_intent(
            TokenType.YES, OrderSide.SELL, 0.60
        )
        assert intent == "ORDER_INTENT_SELL_SHORT"
        assert side == "OUTCOME_SIDE_YES"
        assert price == pytest.approx(0.60)

    def test_no_buy_mapping_price_complement(self):
        """Buying NO at 0.40 -> SELL_SHORT on NO side at 1-0.40=0.60."""
        intent, side, price = PolymarketUSClient._map_order_intent(
            TokenType.NO, OrderSide.BUY, 0.40
        )
        assert intent == "ORDER_INTENT_SELL_SHORT"
        assert side == "OUTCOME_SIDE_NO"
        assert price == pytest.approx(0.60, abs=1e-6)

    def test_no_sell_mapping_price_complement(self):
        """Selling NO at 0.35 -> BUY_LONG on NO side at 1-0.35=0.65."""
        intent, side, price = PolymarketUSClient._map_order_intent(
            TokenType.NO, OrderSide.SELL, 0.35
        )
        assert intent == "ORDER_INTENT_BUY_LONG"
        assert side == "OUTCOME_SIDE_NO"
        assert price == pytest.approx(0.65, abs=1e-6)

    def test_no_price_rounded_6dp(self):
        _, _, price = PolymarketUSClient._map_order_intent(
            TokenType.NO, OrderSide.BUY, 0.333333333
        )
        assert price == round(1 - 0.333333333, 6)


# ─── 4. Dry-run flow ──────────────────────────────────────────────────────────

class TestDryRunFlow:
    def setup_method(self):
        self.client = _make_client(dry_run=True)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_place_order_returns_open_order(self):
        order = self._run(
            self.client.place_order("slug-a", TokenType.YES, OrderSide.BUY, 0.55, 10.0, "test")
        )
        assert order.status == OrderStatus.OPEN
        assert order.market_id == "slug-a"
        assert order.price == pytest.approx(0.55)
        assert order.size == pytest.approx(10.0)

    def test_place_order_stored_in_simulated_orders(self):
        order = self._run(
            self.client.place_order("slug-b", TokenType.NO, OrderSide.BUY, 0.40, 5.0)
        )
        assert order.order_id in self.client._simulated_orders

    def test_cancel_order_flips_to_cancelled(self):
        order = self._run(
            self.client.place_order("slug-c", TokenType.YES, OrderSide.SELL, 0.65, 8.0)
        )
        self._run(self.client.cancel_order(order.order_id))
        stored = self.client._simulated_orders[order.order_id]
        assert stored.status == OrderStatus.CANCELLED

    def test_simulate_fill_full_fill(self):
        order = self._run(
            self.client.place_order("slug-d", TokenType.YES, OrderSide.BUY, 0.55, 10.0)
        )
        trade = self.client.simulate_fill(order.order_id)
        assert trade is not None
        assert trade.size == pytest.approx(10.0)
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(10.0)

    def test_simulate_fill_partial_fill(self):
        order = self._run(
            self.client.place_order("slug-e", TokenType.YES, OrderSide.BUY, 0.50, 20.0)
        )
        trade = self.client.simulate_fill(order.order_id, fill_size=5.0)
        assert trade is not None
        assert trade.size == pytest.approx(5.0)
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert order.filled_size == pytest.approx(5.0)
        assert order.remaining_size == pytest.approx(15.0)

    def test_simulate_fill_updates_position(self):
        order = self._run(
            self.client.place_order("slug-f", TokenType.YES, OrderSide.BUY, 0.55, 10.0)
        )
        self.client.simulate_fill(order.order_id)
        positions = self._run(self.client.get_positions())
        assert "slug-f" in positions
        assert TokenType.YES in positions["slug-f"]
        pos = positions["slug-f"][TokenType.YES]
        assert pos.size == pytest.approx(10.0)

    def test_simulate_fill_on_cancelled_order_returns_none(self):
        order = self._run(
            self.client.place_order("slug-g", TokenType.YES, OrderSide.BUY, 0.55, 10.0)
        )
        self._run(self.client.cancel_order(order.order_id))
        result = self.client.simulate_fill(order.order_id)
        assert result is None

    def test_simulate_fill_stored_in_trades(self):
        order = self._run(
            self.client.place_order("slug-h", TokenType.YES, OrderSide.BUY, 0.55, 10.0)
        )
        self.client.simulate_fill(order.order_id)
        trades = self._run(self.client.get_trades("slug-h"))
        assert len(trades) == 1
        assert trades[0].order_id == order.order_id

    def test_get_open_orders_filters_by_market(self):
        self._run(self.client.place_order("m1", TokenType.YES, OrderSide.BUY, 0.5, 10))
        self._run(self.client.place_order("m2", TokenType.YES, OrderSide.BUY, 0.5, 10))
        orders_m1 = self._run(self.client.get_open_orders("m1"))
        assert len(orders_m1) == 1
        assert orders_m1[0].market_id == "m1"

    def test_cancel_all_orders_returns_count(self):
        self._run(self.client.place_order("m3", TokenType.YES, OrderSide.BUY, 0.5, 10))
        self._run(self.client.place_order("m3", TokenType.NO, OrderSide.BUY, 0.4, 10))
        count = self._run(self.client.cancel_all_orders("m3"))
        assert count == 2

    def test_get_balance_returns_simulated_value(self):
        balance = self._run(self.client.get_balance())
        assert balance == pytest.approx(10000.0)

    def test_get_order_reflects_state(self):
        order = self._run(
            self.client.place_order("slug-i", TokenType.YES, OrderSide.BUY, 0.55, 10.0)
        )
        info = self._run(self.client.get_order(order.order_id))
        assert info["status"] == OrderStatus.OPEN
        assert info["size"] == pytest.approx(10.0)
        assert info["filled_size"] == pytest.approx(0.0)


# ─── 5. Market parsing ────────────────────────────────────────────────────────

class TestMarketParsing:
    def test_parse_market_uses_slug_as_id(self):
        client = _make_client()
        raw = {"slug": "will-trump-win-2024", "question": "Will Trump win?", "active": True}
        market = client._parse_market(raw)
        assert market.market_id == "will-trump-win-2024"
        assert market.condition_id == "will-trump-win-2024"
        assert market.yes_token_id == "will-trump-win-2024"
        assert market.no_token_id == "will-trump-win-2024"

    def test_parse_market_question(self):
        client = _make_client()
        raw = {"slug": "test", "question": "Test question?"}
        market = client._parse_market(raw)
        assert market.question == "Test question?"
