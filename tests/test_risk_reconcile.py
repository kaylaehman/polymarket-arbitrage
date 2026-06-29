"""Exposure reconciliation: sync the risk manager to REAL Kalshi positions.

Fixes the sticky-reservation limitation of the atomic-placement fix — reservations
made at placement are never released, so without reconciliation the per-market cap
would over-block a market forever once a position resolves. Each sweep we overwrite
`_market_exposure` with what Kalshi actually shows we hold (resolved positions drop
off → their reservation is released; the cap stays honest).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.risk_manager import RiskManager, RiskConfig
from polymarket_client.models import Order, OrderSide, TokenType


def _risk(max_market=15.0):
    return RiskManager(RiskConfig(
        max_position_per_market=max_market, max_global_exposure=1000.0,
        max_daily_loss=100.0, max_drawdown_pct=0.5,
        trade_only_high_volume=False, min_24h_volume=0.0,
        whitelist=[], blacklist=[], kill_switch_enabled=True,
    ))


def test_sync_overwrites_market_exposure_and_global():
    rm = _risk()
    # pretend a prior reservation existed
    rm.update_position("kalshi:OLD", TokenType.NO, 10, 0.9)   # 9.0 reserved
    rm.sync_market_exposures({"kalshi:NEW": 7.2})
    assert rm.get_market_exposure("kalshi:OLD") == 0.0        # released (not in real positions)
    assert rm.get_market_exposure("kalshi:NEW") == pytest.approx(7.2)
    assert rm.state.global_exposure == pytest.approx(7.2)


def test_sync_releases_resolved_position_so_cap_reopens():
    rm = _risk(max_market=8.0)
    rm.update_position("kalshi:M", TokenType.NO, 8, 1.0)      # 8.0 -> at cap
    blocked = Order(order_id="t", market_id="kalshi:M", token_type=TokenType.NO,
                    side=OrderSide.BUY, price=0.9, size=1)
    assert rm.check_order(blocked) is False                   # cap binds (8 + 0.9 > 8)
    # position resolves -> Kalshi shows 0 exposure -> reconcile releases it
    rm.sync_market_exposures({})
    assert rm.check_order(blocked) is True                    # cap reopened


def test_sync_empty_clears_all():
    rm = _risk()
    rm.update_position("kalshi:A", TokenType.YES, 5, 0.5)
    rm.sync_market_exposures({})
    assert rm.get_market_exposure("kalshi:A") == 0.0
    assert rm.state.global_exposure == 0.0


@pytest.mark.asyncio
async def test_kalshi_get_real_market_exposures_parses_positions():
    from kalshi_client.api import KalshiClient
    c = KalshiClient(dry_run=False)
    c._signed_request = AsyncMock(return_value={"market_positions": [
        {"ticker": "KXHIGHNY-26JUN29-B83.5", "market_exposure_dollars": "48.48"},
        {"ticker": "KXZERO", "market_exposure_dollars": "0.0"},   # zero -> excluded
    ]})
    out = await c.get_real_market_exposures()
    assert out == {"kalshi:KXHIGHNY-26JUN29-B83.5": pytest.approx(48.48)}


@pytest.mark.asyncio
async def test_kalshi_get_real_market_exposures_dry_run_empty():
    from kalshi_client.api import KalshiClient
    c = KalshiClient(dry_run=True)
    assert await c.get_real_market_exposures() == {}
