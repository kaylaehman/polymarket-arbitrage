import pytest
from music_intel.edge import compute_edge, required_threshold, EdgeConfig

CFG = EdgeConfig()


def test_threshold_scales_inversely_with_confidence():
    hi = required_threshold(1.0, CFG)
    lo = required_threshold(0.3, CFG)
    assert lo > hi                      # low confidence demands MORE edge
    assert hi == pytest.approx(0.05)


def test_strong_confident_edge_passes():
    # model 0.80 vs market 0.50 -> raw 0.30, net 0.27; conf 0.9 -> thr ~0.065
    r = compute_edge(0.80, 0.90, 0.50, liquidity=5000, days_to_resolution=10, cfg=CFG)
    assert r.passes is True and r.side == "YES"
    assert r.net_edge > r.threshold


def test_low_confidence_blocks_same_edge():
    # same raw edge but confidence below floor -> blocked
    r = compute_edge(0.80, 0.10, 0.50, liquidity=5000, days_to_resolution=10, cfg=CFG)
    assert r.passes is False
    assert any("confidence" in x for x in r.reasons)


def test_thin_liquidity_blocks():
    r = compute_edge(0.80, 0.90, 0.50, liquidity=10, days_to_resolution=10, cfg=CFG)
    assert r.passes is False and any("liquidity" in x for x in r.reasons)


def test_far_resolution_blocks():
    r = compute_edge(0.80, 0.90, 0.50, liquidity=5000, days_to_resolution=999, cfg=CFG)
    assert r.passes is False and any("days_to_resolution" in x for x in r.reasons)


def test_small_edge_below_threshold_blocks():
    # model 0.55 vs 0.50 -> raw 0.05, net 0.02 < threshold -> no
    r = compute_edge(0.55, 0.90, 0.50, liquidity=5000, days_to_resolution=10, cfg=CFG)
    assert r.passes is False and any("net edge" in x for x in r.reasons)


def test_no_side_when_blocked():
    r = compute_edge(0.55, 0.90, 0.50, liquidity=5000, days_to_resolution=10, cfg=CFG)
    assert r.side == "none"
