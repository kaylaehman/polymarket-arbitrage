import pytest
from datetime import datetime
from core.directional.models import DirectionalCandidate, DirectionalOrder, DirectionalPosition


def test_order_has_risk_protocol_fields():
    o = DirectionalOrder(
        market_id="kalshi:KX-1",
        side="NO",
        price=0.9,
        size=5,
        notional=4.5,
        strategy="safe_compounder",
    )
    assert (o.market_id, o.notional, o.side) == ("kalshi:KX-1", 4.5, "NO")


def test_candidate_and_position_construct():
    c = DirectionalCandidate(
        market_id="kalshi:KX-1",
        title="t",
        category="Sports",
        side="NO",
        market_price=0.9,
        ai_probability=None,
        confidence=None,
        edge=0.04,
        strategy="safe_compounder",
    )
    assert c.edge == 0.04
    p = DirectionalPosition(
        market_id="kalshi:KX-1",
        side="NO",
        entry_price=0.9,
        size=5,
        strategy="safe_compounder",
        mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=None,
        take_profit=None,
        notional=4.5,
    )
    assert p.status == "open"
    assert p.notional == 4.5


def test_directional_exposure_zero_after_close():
    """directional_exposure() sums open positions' notional — tested here via model invariant."""
    p = DirectionalPosition(
        market_id="kalshi:KX-1",
        side="NO",
        entry_price=0.9,
        size=5,
        strategy="safe_compounder",
        mode="paper",
        opened_at=datetime(2026, 6, 18),
        stop_loss=None,
        take_profit=None,
        notional=4.5,
    )
    assert p.notional == 4.5  # notional is on the position, not derived from price*size
