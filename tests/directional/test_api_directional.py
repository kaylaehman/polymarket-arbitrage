"""Tests for dashboard /api/directional endpoint — Task 16."""
import pytest
from core.directional.store import DirectionalStore
from dashboard.server import build_directional_payload


def test_null_store_returns_empty():
    """build_directional_payload(None) returns the empty payload dict."""
    payload = build_directional_payload(None)
    assert payload == {"strategies": [], "positions": [], "signals": [], "pnl": {}}


def test_api_shape(tmp_path):
    """A seeded store returns a payload with expected keys."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    payload = build_directional_payload(s)
    assert set(payload) >= {"strategies", "positions", "signals", "pnl"}


def test_api_pnl_has_summary_keys(tmp_path):
    """The pnl field contains the standard summary keys."""
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    payload = build_directional_payload(s)
    assert "open_count" in payload["pnl"]
    assert "total_realized_pnl" in payload["pnl"]


def test_positions_serialized(tmp_path):
    """Positions returned as list of dicts (not DirectionalPosition objects)."""
    from datetime import datetime
    from core.directional.models import DirectionalPosition

    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    pos = DirectionalPosition(
        market_id="kalshi:KX-TEST",
        side="NO",
        entry_price=0.07,
        size=10,
        strategy="safe_compounder",
        mode="paper",
        opened_at=datetime.utcnow(),
        stop_loss=None,
        take_profit=None,
        notional=0.7,
        status="open",
    )
    s.record_position(pos)

    payload = build_directional_payload(s)
    assert len(payload["positions"]) == 1
    assert isinstance(payload["positions"][0], dict)
    assert payload["positions"][0]["market_id"] == "kalshi:KX-TEST"


def test_directional_store_field_on_dashboard_state():
    """DashboardState has a directional_store field that defaults to None."""
    from dashboard.server import DashboardState
    ds = DashboardState()
    assert hasattr(ds, "directional_store")
    assert ds.directional_store is None
