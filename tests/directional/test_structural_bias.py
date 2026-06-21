from datetime import datetime, timezone, timedelta
import pytest
from utils.structural_bias import structural_score


def test_longshot_no_bias_positive():
    # NO at a longshot YES price is favored (repo#1 longshot bias)
    assert structural_score(price=0.10, side="NO", category="Sports") > 0


def test_yes_longshot_disfavored():
    assert structural_score(price=0.10, side="YES", category="Sports") <= 0


def test_category_edge_sports_gt_finance():
    assert structural_score(0.10, "NO", "Sports") > structural_score(0.10, "NO", "Finance")


def test_structural_score_correct_for_longshot():
    """Corrected call structural_score(1 - 0.08, "NO", "Sports") is ~0.10 and clears 0.02.

    Validates C1 fix: passing the NO-side price (1 - yes_mid) instead of yes_mid
    gives the correct positive bias for a longshot market.
    """
    from utils.structural_bias import structural_score
    score = structural_score(1 - 0.08, "NO", "Sports")
    # Expected ~0.10 (bias ≈ 5 cents / 100 + category edge 0.04)
    assert abs(score - 0.102) < 0.005, f"Expected ~0.102, got {score}"
    assert score > 0.02, "corrected Sports longshot score must clear default threshold"


@pytest.mark.asyncio
async def test_structural_score_corrected_strategy_emits_at_default_threshold():
    """Strategy emits a Sports longshot candidate at the default min_structural_score=0.02."""
    from core.directional.strategies.maker_longshot import MakerLongshotStrategy
    from types import SimpleNamespace

    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = SimpleNamespace(
        ticker="KX-SPORTS-LS",
        yes_price=0.08,
        no_price=0.92,
        category="Sports",
        title="Longshot sports market",
        status="open",
        result=None,
        close_time=near_term,
    )
    market.to_unified_market_id = lambda: "kalshi:KX-SPORTS-LS"

    strategy = MakerLongshotStrategy(
        min_structural_score=0.02,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,
    )
    ctx = {"no_ask": lambda ticker: 0.94}
    candidates = await strategy.scan([market], ctx)
    assert len(candidates) == 1, "Sports longshot should emit at default threshold"
    assert candidates[0].edge > 0.02


@pytest.mark.asyncio
async def test_structural_score_corrected_strategy_emits_sports_not_above_high_threshold():
    """Strategy does NOT emit when min_structural_score exceeds the corrected score."""
    from core.directional.strategies.maker_longshot import MakerLongshotStrategy
    from types import SimpleNamespace

    near_term = datetime.now(timezone.utc) + timedelta(days=30)
    market = SimpleNamespace(
        ticker="KX-SPORTS-LS2",
        yes_price=0.08,
        no_price=0.92,
        category="Sports",
        title="Longshot sports market 2",
        status="open",
        result=None,
        close_time=near_term,
    )
    market.to_unified_market_id = lambda: "kalshi:KX-SPORTS-LS2"

    # Threshold set above the corrected Sports score (~0.10) to verify the guard works
    strategy = MakerLongshotStrategy(
        min_structural_score=0.15,
        max_yes_price=0.15,
        price_improvement_cents=1,
        skip_categories=[],
        max_days_to_resolution=9999.0,
    )
    ctx = {"no_ask": lambda ticker: 0.94}
    candidates = await strategy.scan([market], ctx)
    assert candidates == [], "score guard should block emission when threshold > corrected score"
