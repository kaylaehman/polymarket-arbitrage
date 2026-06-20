"""Tests for AiDirectional strategy (Task 10)."""
import pytest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from core.directional.strategies.ai_directional import AiDirectional
from kalshi_client.models import KalshiMarket


def mk(ticker, yes_price, category, vol=9000, close_time=None):
    return KalshiMarket(
        ticker=ticker,
        event_ticker=ticker.split("-")[0],
        series_ticker=ticker.split("-")[0],
        title="x",
        yes_price=yes_price,
        category=category,
        volume=vol,
        close_time=close_time,
    )


class FakeIntel:
    async def evaluate(self, **k):
        sig = SimpleNamespace(
            ai_probability=0.7,
            confidence=0.85,
            direction="bullish",
            edge_vs_market=0.12,
            reasoning="news",
        )
        return SimpleNamespace(signal=sig)


@pytest.mark.asyncio
async def test_emits_yes_on_strong_bullish():
    # Existing tests get a near-future close_time so the efficiency filter passes.
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXCPI-1", 0.58, "Finance", close_time=close)
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    assert len(cands) == 1 and cands[0].side == "YES" and cands[0].confidence == 0.85


@pytest.mark.asyncio
async def test_skips_low_confidence():
    class Weak(FakeIntel):
        async def evaluate(self, **k):
            sig = SimpleNamespace(
                ai_probability=0.5,
                confidence=0.4,
                direction="bullish",
                edge_vs_market=0.2,
                reasoning="",
            )
            return SimpleNamespace(signal=sig)

    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXCPI-1", 0.58, "Finance", close_time=close)
    s = AiDirectional(Weak(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []


@pytest.mark.asyncio
async def test_skips_none_signal():
    class NoneIntel:
        async def evaluate(self, **k):
            return None

    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXCPI-1", 0.58, "Finance", close_time=close)
    s = AiDirectional(NoneIntel(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []


# ── I3: candidate.edge equals raw AI edge (not inflated by structural_score) ──

@pytest.mark.asyncio
async def test_candidate_edge_equals_raw_ai_edge():
    """I3: candidate.edge must equal abs(signal.edge_vs_market), not + structural_score."""
    raw_edge = 0.12
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXCPI-2", 0.58, "Finance", close_time=close)
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    assert len(cands) == 1
    # The edge stored on the candidate must equal the raw AI edge exactly,
    # not raw_edge + structural_score(...).
    assert abs(cands[0].edge - raw_edge) < 1e-9, (
        f"candidate.edge={cands[0].edge} should equal raw_edge={raw_edge}; "
        "structural_score must not be added to sizing edge"
    )


# ── Efficiency filter tests ───────────────────────────────────────────────────


class CountingIntel:
    """FakeIntel that records which market IDs were evaluated."""

    def __init__(self):
        self.evaluated = []

    async def evaluate(self, market_id, **k):
        self.evaluated.append(market_id)
        sig = SimpleNamespace(
            ai_probability=0.7,
            confidence=0.85,
            direction="bullish",
            edge_vs_market=0.12,
            reasoning="news",
        )
        return SimpleNamespace(signal=sig)


@pytest.mark.asyncio
async def test_skips_long_dated_market():
    """A market closing 1000 days out is skipped — evaluate() must NOT be called."""
    intel = CountingIntel()
    far_future = datetime.now(timezone.utc) + timedelta(days=1000)
    m = mk("KXPOPE-2070", 0.10, "Politics", close_time=far_future)
    s = AiDirectional(intel, min_confidence=0.60, min_edge_pct=0.05, max_days_to_resolution=45.0)
    result = await s.scan([m], ctx={})
    assert result == [], "long-dated market should produce no candidate"
    assert intel.evaluated == [], "evaluate() must not be called for long-dated market"


@pytest.mark.asyncio
async def test_evaluates_near_term_market():
    """A market closing in 10 days IS evaluated and can produce a candidate."""
    intel = CountingIntel()
    near = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXELEC-10D", 0.58, "Politics", close_time=near)
    s = AiDirectional(intel, min_confidence=0.60, min_edge_pct=0.05, max_days_to_resolution=45.0)
    result = await s.scan([m], ctx={})
    assert len(result) == 1, "near-term market should produce a candidate"
    assert "kalshi:KXELEC-10D" in intel.evaluated


@pytest.mark.asyncio
async def test_skips_past_close_time():
    """A market whose close_time is in the past is skipped."""
    intel = CountingIntel()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    m = mk("KXOLD-1", 0.50, "Finance", close_time=past)
    s = AiDirectional(intel, min_confidence=0.60, min_edge_pct=0.05, max_days_to_resolution=45.0)
    result = await s.scan([m], ctx={})
    assert result == []
    assert intel.evaluated == [], "evaluate() must not be called for past-dated market"


@pytest.mark.asyncio
async def test_evaluates_none_close_time():
    """A market with close_time=None is NOT over-filtered — it IS evaluated."""
    intel = CountingIntel()
    m = mk("KXUNK-1", 0.58, "Finance", close_time=None)
    s = AiDirectional(intel, min_confidence=0.60, min_edge_pct=0.05, max_days_to_resolution=45.0)
    result = await s.scan([m], ctx={})
    assert len(result) == 1, "market with no close_time should be evaluated"
    assert intel.evaluated != []


@pytest.mark.asyncio
async def test_naive_close_time_treated_as_utc():
    """A naive (tz-unaware) close_time within 45 days is treated as UTC and evaluated."""
    intel = CountingIntel()
    # naive datetime 10 days in the future (no tzinfo)
    naive_close = datetime.utcnow() + timedelta(days=10)
    assert naive_close.tzinfo is None, "must be naive for this test"
    m = mk("KXNAIVE-1", 0.58, "Finance", close_time=naive_close)
    s = AiDirectional(intel, min_confidence=0.60, min_edge_pct=0.05, max_days_to_resolution=45.0)
    result = await s.scan([m], ctx={})
    assert len(result) == 1, "naive near-term market should be evaluated (treated as UTC)"
    assert intel.evaluated != []


@pytest.mark.asyncio
async def test_category_filter_skips_wrong_category():
    """With categories=['Politics'], an 'Other' market is skipped without calling evaluate()."""
    intel = CountingIntel()
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m_other = mk("KXOTHER-1", 0.58, "Other", close_time=close)
    s = AiDirectional(
        intel,
        min_confidence=0.60,
        min_edge_pct=0.05,
        max_days_to_resolution=45.0,
        categories=["Politics"],
    )
    result = await s.scan([m_other], ctx={})
    assert result == []
    assert intel.evaluated == [], "evaluate() must not be called for wrong category"


@pytest.mark.asyncio
async def test_category_filter_passes_matching_category():
    """With categories=['Politics'], a 'Politics' market is evaluated."""
    intel = CountingIntel()
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m_pol = mk("KXPOL-1", 0.58, "Politics", close_time=close)
    s = AiDirectional(
        intel,
        min_confidence=0.60,
        min_edge_pct=0.05,
        max_days_to_resolution=45.0,
        categories=["Politics"],
    )
    result = await s.scan([m_pol], ctx={})
    assert len(result) == 1, "Politics market should pass when categories=['Politics']"
    assert intel.evaluated != []


@pytest.mark.asyncio
async def test_empty_categories_passes_all():
    """With categories=[] (default), category filtering is disabled."""
    intel = CountingIntel()
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXANY-1", 0.58, "Esports", close_time=close)
    s = AiDirectional(
        intel,
        min_confidence=0.60,
        min_edge_pct=0.05,
        max_days_to_resolution=45.0,
        categories=[],
    )
    result = await s.scan([m], ctx={})
    assert len(result) == 1, "any-category market should pass when categories is empty"


@pytest.mark.asyncio
async def test_default_constructor_still_works():
    """Existing construction (no max_days / categories args) still works unchanged."""
    close = datetime.now(timezone.utc) + timedelta(days=10)
    m = mk("KXCPI-1", 0.58, "Finance", close_time=close)
    # Old-style construction — must not raise
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    # With default max_days=45, a 10-day market is evaluated
    assert len(cands) == 1
