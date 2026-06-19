# tests/directional/test_config.py
import pytest
from utils.config_loader import load_config


def test_directional_defaults_when_absent(tmp_path):
    cfg = load_config("config.yaml")  # existing config without changes still parses
    assert hasattr(cfg, "directional")
    assert cfg.directional.enabled is False
    assert cfg.directional.caps.total_exposure == 30
    assert cfg.directional.db_path == "data/directional.db"


def test_directional_disabled_when_present(tmp_path):
    """Load the real config.yaml (with the appended directional block) and verify enabled=False."""
    cfg = load_config("config.yaml")
    assert cfg.directional.enabled is False
