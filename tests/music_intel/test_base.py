"""
Tests for music_intel.sources.base — ChartRecord and ChartDataSource.
No live network; pure unit tests.
"""

import datetime
import pytest

from music_intel.sources.base import ChartDataSource, ChartRecord


# ---------------------------------------------------------------------------
# ChartRecord construction
# ---------------------------------------------------------------------------

class TestChartRecord:
    def test_required_fields(self):
        rec = ChartRecord(
            source="kworb",
            chart="hot100",
            as_of=datetime.date(2026, 6, 27),
            rank=1,
            title="Die With A Smile",
            artist="Lady Gaga & Bruno Mars",
        )
        assert rec.source == "kworb"
        assert rec.chart == "hot100"
        assert rec.rank == 1
        assert rec.title == "Die With A Smile"
        assert rec.artist == "Lady Gaga & Bruno Mars"

    def test_optional_fields_default_none(self):
        rec = ChartRecord(
            source="billboard",
            chart="billboard200",
            as_of=datetime.date(2026, 6, 27),
            rank=5,
            title="Short n' Sweet",
            artist="Sabrina Carpenter",
        )
        assert rec.track_id is None
        assert rec.rank_delta is None
        assert rec.streams_period is None
        assert rec.streams_7day is None
        assert rec.days_on_chart is None
        assert rec.peak is None

    def test_optional_fields_set(self):
        rec = ChartRecord(
            source="kworb",
            chart="spotify_us_daily",
            as_of=datetime.date(2026, 6, 27),
            rank=3,
            title="APT.",
            artist="ROSÉ & Bruno Mars",
            track_id="abc123",
            rank_delta=-2,
            streams_period=4_500_000,
            streams_7day=30_000_000,
            days_on_chart=42,
            peak=1,
        )
        assert rec.track_id == "abc123"
        assert rec.rank_delta == -2
        assert rec.streams_period == 4_500_000
        assert rec.peak == 1

    def test_frozen_immutable(self):
        rec = ChartRecord(
            source="kworb",
            chart="hot100",
            as_of=datetime.date(2026, 6, 27),
            rank=1,
            title="Test",
            artist="Artist",
        )
        with pytest.raises((AttributeError, TypeError)):
            rec.rank = 99  # type: ignore[misc]

    def test_equality(self):
        kwargs = dict(
            source="kworb",
            chart="hot100",
            as_of=datetime.date(2026, 6, 27),
            rank=1,
            title="Test",
            artist="Artist",
        )
        assert ChartRecord(**kwargs) == ChartRecord(**kwargs)


# ---------------------------------------------------------------------------
# ChartDataSource — trust_tier ordering via a concrete stub
# ---------------------------------------------------------------------------

class _StubSource(ChartDataSource):
    """Minimal concrete source for interface testing."""

    def __init__(self, name_: str, tier: int):
        self._name = name_
        self._tier = tier

    @property
    def name(self) -> str:
        return self._name

    @property
    def trust_tier(self) -> int:
        return self._tier

    async def fetch(self, chart: str, as_of=None) -> list:
        return []


class TestChartDataSourceInterface:
    def test_kworb_tier_lower_than_billboard(self):
        kworb = _StubSource("kworb", 1)
        billboard = _StubSource("billboard", 2)
        assert kworb.trust_tier < billboard.trust_tier

    def test_billboard_tier_lower_than_luminate(self):
        billboard = _StubSource("billboard", 2)
        luminate = _StubSource("luminate", 3)
        assert billboard.trust_tier < luminate.trust_tier

    def test_tier_ordering_end_to_end(self):
        sources = [
            _StubSource("luminate", 3),
            _StubSource("kworb", 1),
            _StubSource("billboard", 2),
        ]
        ranked = sorted(sources, key=lambda s: s.trust_tier)
        assert [s.name for s in ranked] == ["kworb", "billboard", "luminate"]

    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            ChartDataSource()  # type: ignore[abstract]

    def test_name_property(self):
        src = _StubSource("kworb", 1)
        assert src.name == "kworb"
