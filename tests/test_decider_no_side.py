"""Decider NO-side Kelly sizing — regression for the bug where market_price (the
NO entry cost) was double-flipped, sizing every NO+Kelly bet to 0 (e.g. the
artist_paper Bad Bunny NO bet never placed)."""
import pytest
from unittest.mock import MagicMock
from core.directional.decider import Decider
from core.directional.models import DirectionalCandidate


def _decider(cash=100.0, max_pos=8.0):
    rm = MagicMock(); rm.check_directional_order = MagicMock(return_value=True)
    store = MagicMock()
    store.open_positions = MagicMock(return_value=[])
    store.pending_positions = MagicMock(return_value=[])
    store.directional_exposure = MagicMock(return_value=0.0)
    caps = MagicMock(max_position=max_pos, total_exposure=1e6, max_open=10000, max_open_longshot=10000)
    return Decider(risk_manager=rm, store=store, kelly_frac=0.25,
                   max_position_usd=max_pos, cash_balance_fn=lambda: cash, caps=caps)


def test_no_side_kelly_places_order_at_no_entry_price():
    d = _decider()
    c = DirectionalCandidate(market_id="pm:995702", title="Top Spotify Artist 2026: Bad Bunny",
                             category="music", side="NO", market_price=0.165,
                             ai_probability=0.45, confidence=0.03, edge=0.385, strategy="artist_paper")
    order = d.decide(c)
    assert order is not None, "NO+Kelly bet must size > 0 and place"
    assert order.side == "NO"
    assert order.price == pytest.approx(0.165)   # entry recorded at the NO cost
    assert order.size >= 1


def test_none_confidence_does_not_crash():
    d = _decider()
    c = DirectionalCandidate(market_id="pm:1", title="x", category="music", side="NO",
                             market_price=0.165, ai_probability=0.45, confidence=None,
                             edge=0.385, strategy="artist_paper")
    # must not raise (kelly guards None confidence)
    d.decide(c)
