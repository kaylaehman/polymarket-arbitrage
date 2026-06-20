# tests/directional/test_config.py
import pytest
import yaml
from utils.config_loader import load_config, DirectionalConfig, ConfigError


def test_directional_defaults_when_absent(tmp_path):
    # DirectionalConfig defaults to disabled with expected caps/db path, independent
    # of the live config.yaml (which may be enabled for a paper-validation run).
    from utils.config_loader import DirectionalConfig
    dc = DirectionalConfig()
    assert dc.enabled is False
    assert dc.caps.total_exposure == 30
    assert dc.db_path == "data/directional.db"


def test_directional_enabled_parses_from_block(tmp_path):
    # Parser reads enabled true/false from a directional block correctly, using
    # controlled input (not the live config.yaml value, which varies operationally).
    from utils.config_loader import _build_directional
    assert _build_directional({"enabled": False}).enabled is False
    assert _build_directional({"enabled": True}).enabled is True


# ── M1: min_volume field present on DirectionalConfig ─────────────────────────

def test_directional_config_has_min_volume():
    """M1: DirectionalConfig must expose min_volume (default 100)."""
    cfg = DirectionalConfig()
    assert hasattr(cfg, "min_volume"), "DirectionalConfig must have min_volume field"
    assert cfg.min_volume == 100


def test_min_volume_loaded_from_yaml(tmp_path):
    """M1: min_volume in config.yaml directional block is read by load_config."""
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "min_volume": 500,
        }
    }
    config_file.write_text(yaml.dump(data))
    cfg = load_config(str(config_file))
    assert cfg.directional.min_volume == 500


# ── Fix 3: markets_per_cycle and scan_interval defaults ───────────────────────

def test_directional_markets_per_cycle_default_is_15():
    """Fix 3: markets_per_cycle default must be 15 to cap Claude/news API load per cycle."""
    cfg = DirectionalConfig()
    assert cfg.markets_per_cycle == 15, (
        f"markets_per_cycle default should be 15, got {cfg.markets_per_cycle}"
    )


def test_directional_scan_interval_default_is_300():
    """Fix 3: scan_interval_seconds default must be 300 (5 min) to reduce per-hour API cost."""
    cfg = DirectionalConfig()
    assert cfg.scan_interval_seconds == 300, (
        f"scan_interval_seconds default should be 300, got {cfg.scan_interval_seconds}"
    )


# ── M4: mode fields validated — typos must not be treated as non-paper ─────────

def test_invalid_directional_mode_raises_config_error(tmp_path):
    """M4: A typo'd mode like 'lve' must raise ConfigError, not silently proceed."""
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "ai_directional": {"mode": "lve"},  # typo
        }
    }
    config_file.write_text(yaml.dump(data))
    with pytest.raises(ConfigError, match="mode"):
        load_config(str(config_file))


def test_invalid_safe_compounder_mode_raises_config_error(tmp_path):
    """M4: A typo'd safe_compounder mode must also raise ConfigError."""
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "safe_compounder": {"mode": "Papr"},  # typo
        }
    }
    config_file.write_text(yaml.dump(data))
    with pytest.raises(ConfigError, match="mode"):
        load_config(str(config_file))


def test_negative_cap_raises_config_error(tmp_path):
    """M4: Negative caps must raise ConfigError."""
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "caps": {"max_position": -5},
        }
    }
    config_file.write_text(yaml.dump(data))
    with pytest.raises(ConfigError, match="positive"):
        load_config(str(config_file))


def test_valid_modes_do_not_raise(tmp_path):
    """M4: 'paper' and 'live' are both valid mode values."""
    config_file = tmp_path / "config.yaml"
    data = {
        "directional": {
            "enabled": False,
            "ai_directional": {"mode": "live"},
            "safe_compounder": {"mode": "paper"},
        }
    }
    config_file.write_text(yaml.dump(data))
    cfg = load_config(str(config_file))  # must not raise
    assert cfg.directional.ai_directional.mode == "live"
    assert cfg.directional.safe_compounder.mode == "paper"
