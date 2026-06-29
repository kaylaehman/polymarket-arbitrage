import datetime
import pytest
from music_intel.sources.base import ChartRecord
from music_intel.projection import (
    equivalent_units, project_number_one, track_key, DEFAULT_STREAM_EU,
)

D = datetime.date(2026, 6, 28)


def _rec(artist, title, streams7):
    return ChartRecord(source="kworb", chart="hot100", as_of=D, rank=1,
                       title=title, artist=artist, streams_7day=streams7)


def test_equivalent_units_streaming():
    r = _rec("A", "x", 1_250_000)
    assert equivalent_units(r, stream_eu=1250.0) == pytest.approx(1000.0)


def test_track_key_normalizes():
    assert track_key("Olivia Rodrigo", "Drop Dead") == "olivia rodrigo - drop dead"


def test_clear_leader_high_prob_high_confidence():
    recs = [_rec("A", "x", 12_000_000), _rec("B", "y", 3_000_000),
            _rec("C", "z", 2_000_000)] + [_rec(f"D{i}", "q", 1_000_000) for i in range(8)]
    p = project_number_one(recs, "A", "x")
    assert p.projected_rank == 1
    assert p.prob > 0.9
    assert p.confidence > 0.5
    assert p.prob_low <= p.prob <= p.prob_high


def test_tight_race_low_confidence_wide_band():
    # two near-equal leaders -> low confidence, wide band
    recs = [_rec("A", "x", 5_010_000), _rec("B", "y", 5_000_000)] + \
           [_rec(f"D{i}", "q", 1_000_000) for i in range(8)]
    p = project_number_one(recs, "A", "x")
    assert p.confidence < 0.5                      # tight race -> unconfident
    assert (p.prob_high - p.prob_low) > 0.3        # wide band
    assert 0.4 < p.prob < 0.6                       # near coin-flip


def test_target_absent_low_confidence():
    recs = [_rec("A", "x", 5_000_000), _rec("B", "y", 4_000_000)]
    p = project_number_one(recs, "Nobody", "missing")
    assert p.projected_rank == 0
    assert p.point_estimate_units == 0.0
    assert p.confidence < 0.3                       # never seen -> not confident
    assert p.prob < 0.5


def test_drivers_are_explainable():
    recs = [_rec("A", "x", 6_000_000), _rec("B", "y", 2_000_000)]
    p = project_number_one(recs, "A", "x")
    names = [d[0] for d in p.drivers]
    assert "unit_margin" in names and "target_units" in names and "field_size" in names
