"""Wiring test: ConsensusDivergenceCfg loads from config.yaml and DirectionalConfig exposes it."""

from utils.config_loader import load_config


def test_consensus_divergence_config_parses():
    c = load_config()
    cd = c.directional.consensus_divergence
    assert cd.enabled is True
    assert cd.min_divergence == 0.12
    assert cd.max_yes_price == 0.95
    assert isinstance(cd.skip_categories, list)
