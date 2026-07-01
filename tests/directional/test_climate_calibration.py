"""Tests for climate calibration logging (store + tracker hook)."""

from datetime import datetime

from core.directional.store import DirectionalStore
from core.directional.models import DirectionalCandidate, DirectionalPosition
from core.directional.tracker import Tracker


def test_record_and_read_calibration(tmp_path):
    s = DirectionalStore(str(tmp_path / "d.db"))
    s.init_schema()
    s.record_calibration("kalshi:KXTEMPNYCH-26JUN3017-T92.99", "climate_paper", 0.02, 0)
    rows = s._conn.execute(
        "SELECT strategy,p_yes,outcome_yes FROM climate_calibration"
    ).fetchall()
    assert len(rows) == 1 and rows[0]["strategy"] == "climate_paper"


def test_tracker_records_calibration_on_climate_settlement(tmp_path):
    market_id = "kalshi:KXTEMPNYCH-26JUN3017-T92.99"
    store = DirectionalStore(str(tmp_path / "d.db"))
    store.init_schema()

    # Placed signal with the predicted probability used at entry.
    store.record_signal(
        DirectionalCandidate(
            market_id=market_id,
            title="test",
            category="weather",
            side="NO",
            market_price=0.03,
            ai_probability=0.02,
            confidence=0.9,
            edge=0.01,
            strategy="climate_paper",
            reasoning="",
        ),
        placed=True,
    )

    position = DirectionalPosition(
        market_id=market_id,
        side="NO",
        entry_price=0.03,
        size=5,
        strategy="climate_paper",
        mode="paper",
        opened_at=datetime(2026, 6, 18, 0, 0, 0),
        stop_loss=None,
        take_profit=None,
        notional=0.15,
    )
    store.record_position(position)

    tracker = Tracker(store, kalshi_client=None, executor=None, risk_manager=None)
    tracker._record_calibration_if_climate(position, "no")

    rows = store._conn.execute(
        "SELECT market_id, strategy, p_yes, outcome_yes FROM climate_calibration"
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["market_id"] == market_id
    assert row["strategy"] == "climate_paper"
    assert row["p_yes"] == 0.02
    assert row["outcome_yes"] == 0
