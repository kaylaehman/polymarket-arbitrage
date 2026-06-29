import datetime
import pytest
from types import SimpleNamespace
from music_intel.sources.billboard import BillboardSource


def _entry(rank, title, artist, peak, weeks, last):
    return SimpleNamespace(rank=rank, title=title, artist=artist,
                           peakPos=peak, weeks=weeks, lastPos=last)


@pytest.mark.asyncio
async def test_billboard_maps_entries(monkeypatch):
    src = BillboardSource()
    fake = SimpleNamespace(entries=[
        _entry(1, "Drop Dead", "Olivia Rodrigo", 1, 3, 2),   # last 2 -> delta -1 (up)
        _entry(2, "SWIM", "BTS", 2, 1, 0),                   # last 0 -> delta None (new)
    ])
    monkeypatch.setattr(src, "_fetch_chart", lambda name, d: fake)
    recs = await src.fetch("hot100", as_of=datetime.date(2026, 6, 28))
    assert len(recs) == 2
    assert recs[0].rank == 1 and recs[0].title == "Drop Dead" and recs[0].artist == "Olivia Rodrigo"
    assert recs[0].peak == 1 and recs[0].days_on_chart == 3 and recs[0].rank_delta == -1
    assert recs[1].rank_delta is None  # lastPos 0 -> new entry


def test_billboard_trust_tier_is_2():
    assert BillboardSource().trust_tier == 2 and BillboardSource().name == "billboard"


@pytest.mark.asyncio
async def test_billboard_unknown_chart_empty():
    assert await BillboardSource().fetch("not-a-chart") == []


@pytest.mark.asyncio
async def test_billboard_error_returns_empty(monkeypatch):
    src = BillboardSource()
    def boom(name, d): raise RuntimeError("billboard down")
    monkeypatch.setattr(src, "_fetch_chart", boom)
    assert await src.fetch("hot100") == []
