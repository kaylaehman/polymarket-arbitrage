from utils.edge_filter import passes_edge


def test_tiers():
    assert passes_edge(0.85, 0.04) is True
    assert passes_edge(0.85, 0.02) is False
    assert passes_edge(0.65, 0.06) is True
    assert passes_edge(0.65, 0.04) is False
    assert passes_edge(0.50, 0.09) is True
    assert passes_edge(0.30, 0.50) is False  # below floor
