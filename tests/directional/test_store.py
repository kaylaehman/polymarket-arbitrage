# tests/directional/test_store.py
import pytest
from datetime import datetime
from core.directional.store import DirectionalStore
from core.directional.models import DirectionalPosition


def test_roundtrip(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    pos = DirectionalPosition(
        market_id="kalshi:KX-1", side="NO", entry_price=0.9, size=5,
        strategy="safe_compounder", mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=None, take_profit=None, notional=4.5
    )
    pid = s.record_position(pos)
    assert pid > 0
    assert len(s.open_positions()) == 1
    assert s.directional_exposure() == pytest.approx(4.5)
    s.update_position("kalshi:KX-1", status="closed")
    assert len(s.open_positions()) == 0


def test_exposure_zero_after_close(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    pos = DirectionalPosition("kalshi:KX-1", "NO", 0.9, 5, "safe_compounder", "paper",
                               datetime(2026, 6, 18), None, None, notional=4.5)
    s.record_position(pos)
    s.update_position("kalshi:KX-1", status="closed")
    assert s.directional_exposure() == 0.0
