"""V2 create/cancel-order migration (Kalshi retired v1 POST /portfolio/orders → 410).

The V2 endpoint (POST /portfolio/events/orders) expresses everything on the YES
leg: side ∈ {bid, ask}, fixed-point dollar STRING price, no yes/no or action
fields.  Buying a NO contract = SELLING YES at (1 - no_price).  These tests pin
that mapping exactly — getting NO backwards on a live bundle-arb turns a riskless
lock into a real loss.
"""
import pytest
from unittest.mock import AsyncMock

from kalshi_client.api import KalshiClient
from polymarket_client.models import TokenType, OrderSide, OrderStatus


def _client():
    c = KalshiClient(dry_run=False)
    c._signed_request = AsyncMock(return_value={
        "order_id": "kx-123", "client_order_id": "cid",
        "fill_count": "0", "remaining_count": "8",
    })
    return c


async def _body_for(token_type, side, price=0.94, size=8):
    c = _client()
    await c.place_order(ticker="KXHIGHNY-26JUN30-B70", token_type=token_type,
                        side=side, price=price, size=size)
    method, endpoint = c._signed_request.call_args.args[0], c._signed_request.call_args.args[1]
    body = c._signed_request.call_args.kwargs["json_data"]
    return method, endpoint, body


@pytest.mark.asyncio
async def test_endpoint_is_v2_events_path():
    method, endpoint, _ = await _body_for(TokenType.YES, OrderSide.BUY)
    assert method == "POST"
    assert endpoint == "/portfolio/events/orders"


@pytest.mark.asyncio
async def test_buy_yes_is_bid_at_price():
    _, _, b = await _body_for(TokenType.YES, OrderSide.BUY, price=0.94)
    assert b["side"] == "bid"
    assert b["price"] == "0.9400"
    assert b["count"] == "8"
    assert b["ticker"] == "KXHIGHNY-26JUN30-B70"
    assert b["time_in_force"] == "good_till_canceled"
    assert b["self_trade_prevention_type"] == "taker_at_cross"
    assert "action" not in b and "yes_price" not in b and "no_price" not in b


@pytest.mark.asyncio
async def test_buy_no_is_ask_at_one_minus_price():
    # Buy NO @ 0.94  ==  sell YES @ 0.06
    _, _, b = await _body_for(TokenType.NO, OrderSide.BUY, price=0.94)
    assert b["side"] == "ask"
    assert b["price"] == "0.0600"


@pytest.mark.asyncio
async def test_sell_yes_is_ask_at_price():
    _, _, b = await _body_for(TokenType.YES, OrderSide.SELL, price=0.30)
    assert b["side"] == "ask"
    assert b["price"] == "0.3000"


@pytest.mark.asyncio
async def test_sell_no_is_bid_at_one_minus_price():
    _, _, b = await _body_for(TokenType.NO, OrderSide.SELL, price=0.30)
    assert b["side"] == "bid"
    assert b["price"] == "0.7000"


@pytest.mark.asyncio
async def test_float_noise_does_not_leak_into_price():
    # 1 - 0.93 = 0.07000000000000006 in float; must serialize clean.
    _, _, b = await _body_for(TokenType.NO, OrderSide.BUY, price=0.93)
    assert b["price"] == "0.0700"


@pytest.mark.asyncio
async def test_full_fill_marks_filled():
    c = _client()
    c._signed_request = AsyncMock(return_value={
        "order_id": "kx-9", "fill_count": "8", "remaining_count": "0"})
    o = await c.place_order(ticker="KXT", token_type=TokenType.NO,
                            side=OrderSide.BUY, price=0.9, size=8)
    assert o.status == OrderStatus.FILLED
    assert o.order_id == "kx-9"


@pytest.mark.asyncio
async def test_resting_marks_open():
    c = _client()  # default mock: fill 0, remaining 8
    o = await c.place_order(ticker="KXT", token_type=TokenType.NO,
                            side=OrderSide.BUY, price=0.9, size=8)
    assert o.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_out_of_range_price_rejected():
    c = _client()
    with pytest.raises(ValueError):
        # Buy NO @ 0.005 -> YES leg 0.995 -> 100 cents -> invalid
        await c.place_order(ticker="KXT", token_type=TokenType.NO,
                            side=OrderSide.BUY, price=0.005, size=8)


@pytest.mark.asyncio
async def test_cancel_uses_v2_events_path():
    c = KalshiClient(dry_run=False)
    c._signed_request = AsyncMock(return_value={})
    await c.cancel_order("ord-42")
    method, endpoint = c._signed_request.call_args.args[0], c._signed_request.call_args.args[1]
    assert method == "DELETE"
    assert endpoint == "/portfolio/events/orders/ord-42"
