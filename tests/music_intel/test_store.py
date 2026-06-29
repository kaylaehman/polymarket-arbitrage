"""
Tests for music_intel.store.MusicStore.
All tests use tmp_path (in-memory or temp file); no live network.
"""

import datetime
import json
import pytest

from music_intel.sources.base import ChartRecord
from music_intel.store import MusicStore


TODAY = datetime.date(2026, 6, 27)


def _sample_record(**overrides) -> ChartRecord:
    defaults = dict(
        source="kworb",
        chart="hot100",
        as_of=TODAY,
        rank=1,
        title="Die With A Smile",
        artist="Lady Gaga & Bruno Mars",
        track_id="t1",
        rank_delta=0,
        streams_period=5_000_000,
        streams_7day=35_000_000,
        days_on_chart=10,
        peak=1,
    )
    defaults.update(overrides)
    return ChartRecord(**defaults)


# ---------------------------------------------------------------------------
# Schema init
# ---------------------------------------------------------------------------

class TestInitSchema:
    def test_init_schema_idempotent(self, tmp_path):
        db = str(tmp_path / "music.db")
        store = MusicStore(db)
        store.init_schema()
        store.init_schema()  # second call must not raise

    def test_in_memory(self):
        store = MusicStore(":memory:")
        store.init_schema()


# ---------------------------------------------------------------------------
# chart_snapshots
# ---------------------------------------------------------------------------

class TestChartSnapshots:
    def test_record_and_get(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        rec = _sample_record()
        store.record_snapshot(rec)
        rows = store.get_snapshots("kworb", "hot100", TODAY)
        assert len(rows) == 1
        r = rows[0]
        assert r.rank == 1
        assert r.title == "Die With A Smile"
        assert r.artist == "Lady Gaga & Bruno Mars"
        assert r.streams_period == 5_000_000
        assert r.peak == 1

    def test_get_empty_returns_empty_list(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        result = store.get_snapshots("kworb", "hot100", TODAY)
        assert result == []

    def test_unique_constraint_deduplication(self, tmp_path):
        """Inserting the same source+chart+as_of+rank twice should not raise
        and should not create duplicate rows."""
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        rec = _sample_record()
        store.record_snapshot(rec)
        store.record_snapshot(rec)  # duplicate — must not raise
        rows = store.get_snapshots("kworb", "hot100", TODAY)
        assert len(rows) == 1

    def test_multiple_ranks_stored(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        for i in range(1, 4):
            store.record_snapshot(_sample_record(rank=i, title=f"Song {i}"))
        rows = store.get_snapshots("kworb", "hot100", TODAY)
        assert len(rows) == 3
        assert {r.rank for r in rows} == {1, 2, 3}

    def test_filter_by_date(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        other_day = datetime.date(2026, 6, 20)
        store.record_snapshot(_sample_record(as_of=TODAY))
        store.record_snapshot(_sample_record(as_of=other_day))
        rows = store.get_snapshots("kworb", "hot100", TODAY)
        assert len(rows) == 1

    def test_optional_fields_round_trip(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        rec = _sample_record(track_id=None, rank_delta=None, streams_7day=None)
        store.record_snapshot(rec)
        rows = store.get_snapshots("kworb", "hot100", TODAY)
        assert rows[0].track_id is None
        assert rows[0].rank_delta is None
        assert rows[0].streams_7day is None


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------

class TestProjections:
    def test_record_and_get(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        store.record_projection(
            market_key="pm:hot100-1",
            chart="hot100",
            as_of=TODAY,
            point_estimate=0.72,
            prob_low=0.60,
            prob_high=0.84,
            confidence=0.8,
            drivers_json=json.dumps(["streams", "airplay"]),
            model_prob=0.72,
        )
        rows = store.get_projections("hot100", TODAY)
        assert len(rows) == 1
        assert rows[0]["market_key"] == "pm:hot100-1"
        assert rows[0]["point_estimate"] == pytest.approx(0.72)
        assert rows[0]["confidence"] == pytest.approx(0.8)

    def test_get_projections_empty(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        assert store.get_projections("hot100", TODAY) == []


# ---------------------------------------------------------------------------
# calibration_curves
# ---------------------------------------------------------------------------

class TestCalibrationCurves:
    def test_save_and_get(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        curve = {"bins": [0.1, 0.5, 0.9], "fractions": [0.08, 0.52, 0.88]}
        store.save_calibration("v1", json.dumps(curve))
        result = store.get_calibration("v1")
        assert result is not None
        loaded = json.loads(result)
        assert loaded["bins"] == [0.1, 0.5, 0.9]

    def test_get_calibration_missing_returns_none(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        assert store.get_calibration("nonexistent") is None

    def test_save_calibration_upsert(self, tmp_path):
        """Saving the same version twice should update, not duplicate."""
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        store.save_calibration("v1", json.dumps({"brier": 0.1}))
        store.save_calibration("v1", json.dumps({"brier": 0.05}))
        result = store.get_calibration("v1")
        loaded = json.loads(result)
        assert loaded["brier"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# market_matches
# ---------------------------------------------------------------------------

class TestMarketMatches:
    def test_record_and_recent(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        store.record_match(
            market_id="pm:abc",
            question="Will 'Die With A Smile' hit #1?",
            model_prob=0.72,
            market_prob=0.60,
            edge=0.12,
        )
        matches = store.recent_matches(limit=10)
        assert len(matches) == 1
        m = matches[0]
        assert m["market_id"] == "pm:abc"
        assert m["model_prob"] == pytest.approx(0.72)
        assert m["edge"] == pytest.approx(0.12)

    def test_recent_matches_empty(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        assert store.recent_matches(10) == []

    def test_recent_matches_limit(self, tmp_path):
        store = MusicStore(str(tmp_path / "music.db"))
        store.init_schema()
        for i in range(5):
            store.record_match(
                market_id=f"pm:{i}",
                question=f"Q{i}",
                model_prob=0.5,
                market_prob=0.4,
                edge=0.1,
            )
        assert len(store.recent_matches(3)) == 3
        assert len(store.recent_matches(10)) == 5
