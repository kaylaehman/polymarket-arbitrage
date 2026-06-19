# tests/directional/test_risk_directional.py
import pytest
from core.risk_manager import RiskManager, RiskConfig
from core.directional.models import DirectionalOrder


def _o(notional):
    return DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, notional, "safe_compounder")


def test_directional_caps():
    rm = RiskManager(RiskConfig())
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=0, max_position=8, max_total=30, max_open=4) is True
    assert rm.check_directional_order(_o(9), open_count=0, directional_exposure=0, max_position=8, max_total=30, max_open=4) is False   # > per-position
    assert rm.check_directional_order(_o(8), open_count=0, directional_exposure=25, max_position=8, max_total=30, max_open=4) is False  # > total
    assert rm.check_directional_order(_o(8), open_count=4, directional_exposure=0, max_position=8, max_total=30, max_open=4) is False   # too many open


def test_directional_respects_kill_switch():
    rm = RiskManager(RiskConfig())
    rm._trigger_kill_switch("test")
    assert rm.check_directional_order(_o(1), 0, 0, max_position=8, max_total=30, max_open=4) is False
