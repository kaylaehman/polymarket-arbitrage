# tests/directional/test_safe_compounder.py
"""Tests for Strategy ABC and SafeCompounder (Task 9).

Mocks the no_ask ctx function — no live API calls.
"""
import pytest
from core.directional.strategies.base import Strategy
from core.directional.strategies.safe_compounder import SafeCompounder
from core.directional.models import DirectionalCandidate
from kalshi_client.models import KalshiMarket


def mk(ticker, yes=0.04, vol=5000, category="Sports"):
    """Build a minimal KalshiMarket for testing."""
    et = ticker.split("-")[0]
    m = KalshiMarket(
        ticker=ticker,
        event_ticker=et,
        series_ticker=et,
        title=ticker,
        yes_price=yes,
        no_price=round(1.0 - yes, 4),
        volume=vol,
    )
    m.category = category
    return m


# ---------------------------------------------------------------------------
# Strategy ABC contract
# ---------------------------------------------------------------------------

def test_strategy_is_abstract():
    """Strategy cannot be instantiated directly — must be subclassed."""
    import inspect
    assert inspect.isabstract(Strategy)


def test_safe_compounder_is_strategy():
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    assert isinstance(sc, Strategy)


def test_safe_compounder_name():
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    assert sc.name == "safe_compounder"


# ---------------------------------------------------------------------------
# SafeCompounder.scan: edge calculation and candidate emission
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emits_no_candidate_on_cheap_no():
    """YES at 0.04 → fair_no ≈ 0.96; no_ask 0.93 → edge = 3c → emit NO candidate."""
    m = mk("KXMLB-1", yes=0.04)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert len(cands) == 1
    assert cands[0].side == "NO"


@pytest.mark.asyncio
async def test_candidate_price_is_ask_minus_one_cent():
    """Resting limit price = no_ask − 0.01."""
    m = mk("KXMLB-1", yes=0.04)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert len(cands) == 1
    assert abs(cands[0].market_price - 0.92) < 1e-9


@pytest.mark.asyncio
async def test_skips_when_edge_too_small():
    """YES at 0.04 → fair_no ≈ 0.96; no_ask 0.97 → edge ≈ −1c < 3c → skip."""
    m = mk("KXMLB-1", yes=0.04)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.97})
    assert cands == []


@pytest.mark.asyncio
async def test_skips_excluded_category():
    """Markets whose category is in skip_categories are ignored."""
    m = mk("KXENT-1", yes=0.04, category="Entertainment")
    sc = SafeCompounder(min_edge_cents=3, skip_categories=["Entertainment"])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert cands == []


@pytest.mark.asyncio
async def test_skips_when_no_ask_not_available():
    """ctx no_ask returns None → skip market (no order book data)."""
    m = mk("KXMLB-1", yes=0.04)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: None})
    assert cands == []


@pytest.mark.asyncio
async def test_candidate_strategy_field():
    """DirectionalCandidate.strategy == 'safe_compounder'."""
    m = mk("KXMLB-1", yes=0.04)
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert cands[0].strategy == "safe_compounder"


@pytest.mark.asyncio
async def test_candidate_category_propagated():
    """DirectionalCandidate.category comes from market.category."""
    m = mk("KXMLB-1", yes=0.04, category="Sports")
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan([m], ctx={"no_ask": lambda t: 0.93})
    assert cands[0].category == "Sports"


@pytest.mark.asyncio
async def test_multiple_markets_filtered_independently():
    """Edge check applied per market; only qualifying markets produce candidates."""
    markets = [
        mk("KXMLB-1", yes=0.04),   # fair_no=0.96, ask=0.93 → edge=3c → emit
        mk("KXMLB-2", yes=0.04),   # fair_no=0.96, ask=0.97 → edge=-1c → skip
    ]
    ask_map = {"KXMLB-1": 0.93, "KXMLB-2": 0.97}
    sc = SafeCompounder(min_edge_cents=3, skip_categories=[])
    cands = await sc.scan(markets, ctx={"no_ask": lambda t: ask_map.get(t)})
    assert len(cands) == 1
    assert cands[0].market_id == "kalshi:KXMLB-1"
