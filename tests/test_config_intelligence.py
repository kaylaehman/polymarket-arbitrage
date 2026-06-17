"""Tests for Phase 2 wiring: intelligence config parsing + Opportunity.signal."""

import textwrap

import pytest

from utils.config_loader import (
    BotConfig,
    IntelligenceConfig,
    IntelligenceClaudeConfig,
    IntelligenceNewsConfig,
    load_config,
)


def _write(tmp_path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(text))
    return str(path)


def test_defaults_when_section_absent(tmp_path):
    # A config with no `intelligence:` block must still parse (backwards compat).
    cfg_path = _write(tmp_path, """
        mode:
          trading_mode: "dry_run"
          data_mode: "simulation"
    """)
    config = load_config(cfg_path)
    assert isinstance(config.intelligence, IntelligenceConfig)
    assert config.intelligence.enabled is False
    assert config.intelligence.min_edge_filter == 0.10
    assert isinstance(config.intelligence.news, IntelligenceNewsConfig)
    assert isinstance(config.intelligence.claude, IntelligenceClaudeConfig)


def test_full_intelligence_section_parsed(tmp_path):
    cfg_path = _write(tmp_path, """
        mode:
          trading_mode: "dry_run"
        intelligence:
          enabled: true
          mode: "both"
          min_confidence: 0.7
          min_edge_boost: 0.04
          min_edge_filter: 0.12
          max_position_boost: 25
          news:
            lookback_hours: 6
            max_articles: 8
            cache_ttl_minutes: 15
            sources: ["reuters", "bbc-news"]
          claude:
            model: "claude-sonnet-4-6"
            max_tokens: 1024
            timeout_seconds: 10
    """)
    intel = load_config(cfg_path).intelligence
    assert intel.enabled is True
    assert intel.mode == "both"
    assert intel.min_confidence == 0.7
    assert intel.min_edge_filter == 0.12
    assert intel.max_position_boost == 25
    assert intel.news.lookback_hours == 6
    assert intel.news.sources == ["reuters", "bbc-news"]
    assert intel.claude.max_tokens == 1024
    assert intel.claude.timeout_seconds == 10


def test_partial_section_uses_defaults_for_missing(tmp_path):
    cfg_path = _write(tmp_path, """
        intelligence:
          enabled: true
          news:
            lookback_hours: 12
    """)
    intel = load_config(cfg_path).intelligence
    assert intel.enabled is True
    assert intel.news.lookback_hours == 12
    assert intel.news.max_articles == 5          # default preserved
    assert intel.claude.model == "claude-sonnet-4-6"  # default sub-object


def test_default_botconfig_has_intelligence():
    # The dataclass default must exist so code paths that build BotConfig()
    # directly (tests, backtest) don't crash.
    assert isinstance(BotConfig().intelligence, IntelligenceConfig)


def test_opportunity_accepts_signal_field():
    # The new optional field must default to None and accept a SignalSummary.
    from polymarket_client.models import Opportunity, OpportunityType
    from intelligence.signal import SignalSummary

    opp = Opportunity(
        opportunity_id="x",
        opportunity_type=OpportunityType.BUNDLE_LONG,
        market_id="m1",
        edge=0.03,
    )
    assert opp.signal is None

    opp.signal = SignalSummary.neutral(arb_edge=0.03)
    assert opp.signal.adjusted_edge == 0.03
