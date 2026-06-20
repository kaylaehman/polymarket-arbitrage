"""
Tests for ArbConfig.bundle_cooldown_seconds and bundle-arb cooldown enforcement.

FIX-1: bundle_cooldown_seconds replaces the literal timedelta(seconds=2) so the
dedup window is configurable and defaults to 5.0 s, preventing a slow REST sweep
from re-submitting a bundle already handled by the WS detector.
"""

from datetime import datetime, timedelta

import pytest

from polymarket_client.models import (
    Market,
    MarketState,
    OrderBook,
    OrderBookSide,
    PriceLevel,
    TokenOrderBook,
    TokenType,
)
from core.arb_engine import ArbConfig, ArbEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _book_with_bundle_long(market_id: str = "cool_market") -> OrderBook:
    """Return an order book with a clear BUNDLE_LONG opportunity (total ask 0.90)."""
    return OrderBook(
        market_id=market_id,
        yes=TokenOrderBook(
            token_type=TokenType.YES,
            bids=OrderBookSide(levels=[PriceLevel(price=0.43, size=100)]),
            asks=OrderBookSide(levels=[PriceLevel(price=0.45, size=100)]),
        ),
        no=TokenOrderBook(
            token_type=TokenType.NO,
            bids=OrderBookSide(levels=[PriceLevel(price=0.43, size=100)]),
            asks=OrderBookSide(levels=[PriceLevel(price=0.45, size=100)]),
        ),
    )


def _state(order_book: OrderBook) -> MarketState:
    return MarketState(
        market=Market(
            market_id=order_book.market_id,
            condition_id=order_book.market_id,
            question="Cooldown test market",
            active=True,
            volume_24h=50_000.0,
        ),
        order_book=order_book,
    )


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

class TestArbConfigDefaults:
    """ArbConfig.bundle_cooldown_seconds is 5.0 by default."""

    def test_default_bundle_cooldown_is_five(self) -> None:
        cfg = ArbConfig()
        assert cfg.bundle_cooldown_seconds == 5.0

    def test_custom_bundle_cooldown_is_respected(self) -> None:
        cfg = ArbConfig(bundle_cooldown_seconds=10.0)
        assert cfg.bundle_cooldown_seconds == 10.0


# ---------------------------------------------------------------------------
# Engine reads config
# ---------------------------------------------------------------------------

class TestCooldownEnforcement:
    """Engine uses config.bundle_cooldown_seconds for dedup."""

    @pytest.fixture
    def zero_fee_config(self) -> ArbConfig:
        """Config with fees zeroed so edge maths is easy, cooldown at default 5s."""
        return ArbConfig(
            min_edge=0.01,
            bundle_arb_enabled=True,
            mm_enabled=False,
            maker_fee_bps=0,
            taker_fee_bps=0,
            gas_cost_per_order=0.0,
        )

    def test_first_analyze_yields_bundle_signal(self, zero_fee_config: ArbConfig) -> None:
        engine = ArbEngine(zero_fee_config)
        state = _state(_book_with_bundle_long())
        signals = engine.analyze(state)
        bundle = [s for s in signals if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle) >= 1, "Expected at least one bundle signal on first analyze"

    def test_second_analyze_within_cooldown_returns_no_bundle(
        self, zero_fee_config: ArbConfig
    ) -> None:
        """Second call in the same tick (well within 5-s window) must be suppressed."""
        engine = ArbEngine(zero_fee_config)
        state = _state(_book_with_bundle_long())

        first = engine.analyze(state)
        bundle_first = [s for s in first if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_first) >= 1, "Precondition: first call must produce a bundle signal"

        second = engine.analyze(state)
        bundle_second = [s for s in second if s.opportunity and s.opportunity.is_bundle_arb]
        assert bundle_second == [], (
            "Duplicate bundle signal within cooldown window must be suppressed"
        )

    def test_cooldown_expires_after_window(self, zero_fee_config: ArbConfig) -> None:
        """After manually expiring the cooldown, the engine fires again."""
        engine = ArbEngine(zero_fee_config)
        state = _state(_book_with_bundle_long())

        # Arm the cooldown
        engine.analyze(state)

        # Manually expire all cooldown entries
        past = datetime.utcnow() - timedelta(seconds=zero_fee_config.bundle_cooldown_seconds + 1)
        for key in list(engine._opportunity_cooldown):
            engine._opportunity_cooldown[key] = past

        # Now the engine should fire again
        after_expiry = engine.analyze(state)
        bundle_after = [s for s in after_expiry if s.opportunity and s.opportunity.is_bundle_arb]
        assert len(bundle_after) >= 1, (
            "Bundle signal expected after cooldown window has elapsed"
        )

    def test_custom_cooldown_respected(self) -> None:
        """Engine honours a non-default cooldown value."""
        cfg = ArbConfig(
            min_edge=0.01,
            bundle_arb_enabled=True,
            mm_enabled=False,
            maker_fee_bps=0,
            taker_fee_bps=0,
            gas_cost_per_order=0.0,
            bundle_cooldown_seconds=0.0,  # zero — every call fires immediately
        )
        engine = ArbEngine(cfg)
        state = _state(_book_with_bundle_long())

        # With zero cooldown, two back-to-back calls should both produce signals
        first = engine.analyze(state)
        second = engine.analyze(state)

        bundle_first = [s for s in first if s.opportunity and s.opportunity.is_bundle_arb]
        bundle_second = [s for s in second if s.opportunity and s.opportunity.is_bundle_arb]

        assert len(bundle_first) >= 1
        assert len(bundle_second) >= 1, (
            "With bundle_cooldown_seconds=0, cooldown should not suppress the second call"
        )
