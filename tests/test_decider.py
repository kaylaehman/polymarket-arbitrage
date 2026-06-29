"""
Tests for core.directional.decider.Decider.decide() — covers paths NOT already
tested in tests/test_decider_no_side.py (which covers NO-side Kelly and
confidence=None passthrough).

Coverage:
- YES side with ai_probability set -> places an order with correct side/price/size
- ai_probability is None (Safe Compounder) -> sizes from pos_cap, NOT via kelly
- size < 1 (tiny cash + large price) -> returns None
- Kelly returns 0 (non-positive EV) -> notional=0 -> returns None
- Longshot bucket cap (max_open_longshot=0, non-daily market) -> returns None
- Risk gate returning False -> returns None even when sizing succeeds
- Daily/weather market is EXEMPT from longshot bucket cap -> places order
"""
import math
import pytest
from unittest.mock import MagicMock

from core.directional.decider import Decider
from core.directional.models import DirectionalCandidate


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_decider(
    cash: float = 200.0,
    max_pos: float = 10.0,
    max_longshot: int = 10000,
    risk_passes: bool = True,
) -> Decider:
    """Build a Decider with fully mocked boundaries."""
    rm = MagicMock()
    rm.check_directional_order = MagicMock(return_value=risk_passes)

    store = MagicMock()
    store.open_positions = MagicMock(return_value=[])
    store.directional_exposure = MagicMock(return_value=0.0)

    caps = MagicMock(
        max_position=max_pos,
        total_exposure=1_000_000.0,
        max_open=10_000,
        max_open_longshot=max_longshot,
    )

    return Decider(
        risk_manager=rm,
        store=store,
        kelly_frac=0.25,
        max_position_usd=max_pos,
        cash_balance_fn=lambda: cash,
        caps=caps,
    )


def _yes_candidate(
    market_id: str = "kalshi:KXCPI-23-JAN-B",
    market_price: float = 0.3,
    ai_probability: float | None = 0.5,
    confidence: float | None = 0.8,
    edge: float = 0.07,
    side: str = "YES",
) -> DirectionalCandidate:
    return DirectionalCandidate(
        market_id=market_id,
        title="Test market",
        category="macro",
        side=side,
        market_price=market_price,
        ai_probability=ai_probability,
        confidence=confidence,
        edge=edge,
        strategy="test",
    )


# ---------------------------------------------------------------------------
# YES side with ai_probability set
# ---------------------------------------------------------------------------

class TestYesSideKelly:
    """A YES-side candidate with a positive Kelly edge should produce an order."""

    def test_should_return_order_when_yes_side_has_positive_edge(self):
        d = _make_decider()
        c = _yes_candidate()
        order = d.decide(c)
        assert order is not None

    def test_should_record_yes_side_on_order(self):
        d = _make_decider()
        c = _yes_candidate(side="YES")
        order = d.decide(c)
        assert order.side == "YES"

    def test_should_record_market_price_on_order(self):
        d = _make_decider()
        c = _yes_candidate(market_price=0.3)
        order = d.decide(c)
        assert order.price == pytest.approx(0.3)

    def test_should_have_size_of_at_least_one(self):
        d = _make_decider()
        c = _yes_candidate()
        order = d.decide(c)
        assert order.size >= 1

    def test_should_set_notional_consistent_with_size_and_price(self):
        d = _make_decider()
        c = _yes_candidate(market_price=0.3)
        order = d.decide(c)
        assert order.notional == pytest.approx(order.size * 0.3, abs=1e-9)


# ---------------------------------------------------------------------------
# Safe Compounder (ai_probability is None)
# ---------------------------------------------------------------------------

class TestSafeCompounder:
    """When ai_probability is None the decider must use pos_cap sizing directly,
    bypassing Kelly entirely."""

    def test_should_return_order_for_safe_compounder_candidate(self):
        d = _make_decider(cash=200.0, max_pos=10.0)
        c = _yes_candidate(ai_probability=None, confidence=None, edge=0.0,
                           market_price=0.3)
        order = d.decide(c)
        assert order is not None

    def test_should_size_from_pos_cap_not_kelly(self):
        # pos_cap = min(max_position_usd=10, caps.max_position=10) = 10.0
        # size = floor(10.0 / 0.3) = 33
        d = _make_decider(cash=200.0, max_pos=10.0)
        c = _yes_candidate(ai_probability=None, confidence=None, edge=0.0,
                           market_price=0.3)
        order = d.decide(c)
        assert order.size == math.floor(10.0 / 0.3)

    def test_should_use_smaller_of_max_position_usd_and_caps_max_position(self):
        # max_position_usd=5 < caps.max_position=10 -> pos_cap=5
        # size = floor(5 / 0.3) = 16
        rm = MagicMock()
        rm.check_directional_order = MagicMock(return_value=True)
        store = MagicMock()
        store.open_positions = MagicMock(return_value=[])
        store.directional_exposure = MagicMock(return_value=0.0)
        caps = MagicMock(max_position=10.0, total_exposure=1e6, max_open=10000,
                         max_open_longshot=10000)
        d = Decider(risk_manager=rm, store=store, kelly_frac=0.25,
                    max_position_usd=5.0, cash_balance_fn=lambda: 200.0, caps=caps)
        c = _yes_candidate(ai_probability=None, confidence=None, edge=0.0,
                           market_price=0.3)
        order = d.decide(c)
        assert order.size == math.floor(5.0 / 0.3)


# ---------------------------------------------------------------------------
# size < 1 -> None
# ---------------------------------------------------------------------------

class TestSizeTooSmall:
    """When floor(notional / price) < 1 the decider must return None."""

    def test_should_return_none_when_cash_is_too_small_for_one_share(self):
        # cash=0.5, max_pos=0.5 -> notional (safe compounder) = min(0.5, 10) = 0.5
        # size = floor(0.5 / 0.99) = 0 -> None
        d = _make_decider(cash=0.5, max_pos=0.5)
        c = _yes_candidate(ai_probability=None, confidence=None, edge=0.0,
                           market_price=0.99)
        order = d.decide(c)
        assert order is None

    def test_should_return_none_when_kelly_fraction_produces_sub_share_notional(self):
        # Kelly can produce a tiny fraction for marginal edge; with very low cash
        # and a high price the floor may land at 0.
        d = _make_decider(cash=0.01, max_pos=10.0)
        c = _yes_candidate(market_price=0.95, ai_probability=0.96,
                           confidence=0.8, edge=0.01)
        order = d.decide(c)
        assert order is None


# ---------------------------------------------------------------------------
# Kelly returns 0 -> notional=0 -> None
# ---------------------------------------------------------------------------

class TestZeroKellyNotional:
    """When Kelly returns 0 (non-positive EV) the notional is 0 and decide
    returns None — the I2 fix prohibits any fallback sizing."""

    def test_should_return_none_when_ai_probability_below_market_price_at_high_confidence(self):
        # ai_prob=0.5 < yes_price=0.6 at high confidence -> Kelly < 0 -> clamped to 0
        # notional = 0 * cash = 0 -> decide returns None immediately
        d = _make_decider(cash=200.0, max_pos=10.0)
        c = _yes_candidate(market_price=0.6, ai_probability=0.5,
                           confidence=0.9, edge=0.0)
        order = d.decide(c)
        assert order is None

    def test_should_return_none_not_fall_back_to_edge_based_sizing(self):
        # Confirm no fallback: even with edge > 0, if ai_prob << yes_price at
        # high confidence Kelly is still 0 and decide must return None.
        d = _make_decider(cash=200.0, max_pos=10.0)
        # ai_prob=0.1 << yes_price=0.7 -> definitely negative Kelly
        c = _yes_candidate(market_price=0.7, ai_probability=0.1,
                           confidence=0.9, edge=0.05)
        order = d.decide(c)
        assert order is None


# ---------------------------------------------------------------------------
# Longshot bucket cap
# ---------------------------------------------------------------------------

class TestLongshotBucketCap:
    """Non-daily markets are subject to a count cap (caps.max_open_longshot).
    When the bucket is full (open count >= max_open_longshot) decide returns None."""

    def test_should_return_none_for_non_daily_market_when_longshot_bucket_full(self):
        # KXCPI is macro (non-daily); max_open_longshot=0 -> bucket immediately full
        d = _make_decider(max_longshot=0)
        c = _yes_candidate(market_id="kalshi:KXCPI-23-JAN-B")
        order = d.decide(c)
        assert order is None

    def test_should_allow_non_daily_when_bucket_not_full(self):
        # max_open_longshot=1, open_positions returns [] (0 longshots open) -> allowed
        d = _make_decider(max_longshot=1)
        c = _yes_candidate(market_id="kalshi:KXCPI-23-JAN-B")
        order = d.decide(c)
        assert order is not None

    def test_should_return_none_for_pm_music_market_when_longshot_bucket_full(self):
        # pm: prefix maps to "music" (non-daily) so also subject to the longshot cap
        d = _make_decider(max_longshot=0)
        c = _yes_candidate(market_id="pm:99999")
        order = d.decide(c)
        assert order is None


# ---------------------------------------------------------------------------
# Daily/weather market is EXEMPT from longshot cap
# ---------------------------------------------------------------------------

class TestDailyMarketExemption:
    """KXHIGH* maps to 'weather', which equals _DAILY_CATEGORY in decider.
    Daily markets bypass the longshot count cap entirely."""

    def test_should_place_order_for_weather_market_even_when_longshot_bucket_full(self):
        # max_open_longshot=0, but KXHIGH is weather -> exempt -> order placed
        d = _make_decider(max_longshot=0)
        c = _yes_candidate(market_id="kalshi:KXHIGH-23-JAN")
        order = d.decide(c)
        assert order is not None

    def test_should_place_order_for_pmus_temp_market_even_when_longshot_bucket_full(self):
        # pmus:tc-temp-... maps to weather -> daily -> exempt
        d = _make_decider(max_longshot=0)
        c = _yes_candidate(market_id="pmus:tc-temp-chicago-2026-01-23")
        order = d.decide(c)
        assert order is not None

    def test_should_record_correct_market_id_on_daily_order(self):
        d = _make_decider(max_longshot=0)
        c = _yes_candidate(market_id="kalshi:KXHIGH-23-JAN")
        order = d.decide(c)
        assert order.market_id == "kalshi:KXHIGH-23-JAN"


# ---------------------------------------------------------------------------
# Risk gate
# ---------------------------------------------------------------------------

class TestRiskGate:
    """When check_directional_order returns False the decider must return None,
    even though sizing completed successfully."""

    def test_should_return_none_when_risk_gate_rejects(self):
        d = _make_decider(risk_passes=False)
        c = _yes_candidate()
        order = d.decide(c)
        assert order is None

    def test_should_call_risk_manager_with_order(self):
        rm = MagicMock()
        rm.check_directional_order = MagicMock(return_value=False)
        store = MagicMock()
        store.open_positions = MagicMock(return_value=[])
        store.directional_exposure = MagicMock(return_value=0.0)
        caps = MagicMock(max_position=10.0, total_exposure=1e6, max_open=10000,
                         max_open_longshot=10000)
        d = Decider(risk_manager=rm, store=store, kelly_frac=0.25,
                    max_position_usd=10.0, cash_balance_fn=lambda: 200.0, caps=caps)
        c = _yes_candidate()
        d.decide(c)
        rm.check_directional_order.assert_called_once()

    def test_should_return_order_when_risk_gate_passes(self):
        d = _make_decider(risk_passes=True)
        c = _yes_candidate()
        order = d.decide(c)
        assert order is not None
