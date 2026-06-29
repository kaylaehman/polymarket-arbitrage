import pytest
from core.directional.strategies.consensus_divergence import (
    ConsensusDivergenceStrategy, divergence_side,
)

def test_divergence_side_yes_when_gate_higher():
    assert divergence_side(0.40, 0.20, 0.10) == ("YES", pytest.approx(0.20))

def test_divergence_side_no_when_gate_lower():
    side, edge = divergence_side(0.05, 0.20, 0.10)
    assert side == "NO" and edge == pytest.approx(0.15)

def test_divergence_side_none_when_below_threshold():
    assert divergence_side(0.22, 0.20, 0.10) is None

def test_name():
    s = ConsensusDivergenceStrategy(min_divergence=0.1, skip_categories=[])
    assert s.name == "consensus_divergence"

@pytest.mark.asyncio
async def test_scan_no_gate_data_returns_empty():
    s = ConsensusDivergenceStrategy(min_divergence=0.1, skip_categories=[])
    assert await s.scan([], {"no_ask": lambda t: None}) == []
