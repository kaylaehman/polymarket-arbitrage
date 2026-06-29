"""Atomic bundle execution + exposure reservation (real-money safety).

Root cause of the 2026-06-29 one-sided fill incident: ExecutionEngine._handle_place_orders
placed each bundle leg independently (one-sided positions possible) AND never reserved
exposure (the per-market risk cap was toothless, so a bundle re-fired every 5s and
accumulated to $48 instead of stopping at the $15 cap). These tests pin:
  (gap 1) a bundle is ALL-OR-NOTHING — if any leg fails validation, place NONE;
  (gap 2) exposure is reserved on placement so the per-market cap actually binds;
  (gap 3) a re-fire on a market already at the cap places nothing.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.execution import ExecutionEngine, ExecutionConfig
from core.risk_manager import RiskManager, RiskConfig
from polymarket_client.models import Order, OrderSide, TokenType, OrderStatus, Signal


def _risk(max_market=15.0):
    return RiskManager(RiskConfig(
        max_position_per_market=max_market, max_global_exposure=1000.0,
        max_daily_loss=100.0, max_drawdown_pct=0.5,
        trade_only_high_volume=False, min_24h_volume=0.0,
        whitelist=[], blacklist=[], kill_switch_enabled=True,
    ))


class _FakeClient:
    def __init__(self):
        self.placed = []
        self.cancelled = []
    async def place_order(self, market_id, token_type, side, price, size, strategy_tag=""):
        oid = f"o{len(self.placed)}"
        self.placed.append((market_id, token_type, side, price, size))
        return Order(order_id=oid, market_id=market_id, token_type=token_type,
                     side=side, price=price, size=size, strategy_tag=strategy_tag,
                     status=OrderStatus.OPEN)
    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)


def _engine(risk, client=None):
    client = client or _FakeClient()
    eng = ExecutionEngine(
        client=client, risk_manager=risk, portfolio=MagicMock(),
        config=ExecutionConfig(dry_run=False, max_retries=1,
                               enable_slippage_check=False, kelly_enabled=False),
    )
    return eng, client


def _bundle(market, yes_price, yes_size, no_price, no_size):
    return Signal(
        signal_id="s1", action="place_orders", market_id=market, opportunity=None,
        orders=[
            {"token_type": TokenType.YES, "side": OrderSide.BUY, "price": yes_price,
             "size": yes_size, "strategy_tag": "bundle_arb"},
            {"token_type": TokenType.NO, "side": OrderSide.BUY, "price": no_price,
             "size": no_size, "strategy_tag": "bundle_arb"},
        ],
    )


@pytest.mark.asyncio
async def test_gap1_one_leg_over_cap_places_neither():
    # YES notional 4 (ok), NO notional 8 (> cap 5) -> ATOMIC: place NEITHER.
    eng, client = _engine(_risk(max_market=5.0))
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.8, 10))
    assert client.placed == [], "atomic: a rejected leg must place no legs at all"


@pytest.mark.asyncio
async def test_gap1_invalid_price_leg_places_neither():
    # NO price 0.0 -> out of Kalshi 1-99c range -> place NEITHER.
    eng, client = _engine(_risk(max_market=100.0))
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.0, 10))
    assert client.placed == []


@pytest.mark.asyncio
async def test_happy_path_both_legs_placed():
    eng, client = _engine(_risk(max_market=100.0))
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.4, 8))  # 4 + 3.2 = 7.2
    assert len(client.placed) == 2


@pytest.mark.asyncio
async def test_gap2_exposure_reserved_on_placement():
    risk = _risk(max_market=100.0)
    eng, client = _engine(risk)
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.4, 8))
    # exposure = 0.5*8 + 0.4*8 = 4.0 + 3.2 = 7.2
    assert risk.get_market_exposure("kalshi:KXM") == pytest.approx(7.2)


@pytest.mark.asyncio
async def test_gap3_refire_at_cap_places_nothing():
    # cap 8; first bundle reserves 7.2; a second identical bundle would push past 8 -> 0 placed.
    risk = _risk(max_market=8.0)
    eng, client = _engine(risk)
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.4, 8))
    first = len(client.placed)
    await eng._handle_place_orders(_bundle("kalshi:KXM", 0.5, 8, 0.4, 8))  # re-fire
    assert first == 2
    assert len(client.placed) == 2, "re-fire on a market at the cap must place nothing"
