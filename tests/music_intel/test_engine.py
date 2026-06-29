import datetime
import os
import pytest
from music_intel.engine import MusicIntelEngine, ChartSignal, _match_target
from music_intel.config import MusicIntelConfig
from music_intel.alerts import CollectingSink
from music_intel.sources.base import ChartRecord
from music_intel.sources.markets import MarketCandidate

D = datetime.date(2026, 6, 28)


def _rec(artist, title, s7):
    return ChartRecord(source="kworb", chart="hot100", as_of=D, rank=1,
                       title=title, artist=artist, streams_7day=s7)


class _Src:
    def __init__(self, recs, tier=1, name="kworb"):
        self._recs, self._tier, self._name = recs, tier, name
    @property
    def name(self): return self._name
    @property
    def trust_tier(self): return self._tier
    async def fetch(self, chart, as_of=None): return self._recs


def _market(prob_yes, q="Will A Song be #1 on the Billboard Hot 100?", liq=5000, close=None):
    return MarketCandidate(venue="polymarket", market_id="pm:1", question=q,
                           outcomes=["Yes", "No"], prices=[prob_yes, 1 - prob_yes],
                           liquidity=liq,
                           close_time=close or (datetime.datetime(2026, 7, 1, tzinfo=datetime.timezone.utc)),
                           resolution_text="")


def _field():
    return [_rec("A", "Song", 12_000_000), _rec("B", "Other", 3_000_000)] + \
           [_rec(f"D{i}", "x", 1_000_000) for i in range(8)]


@pytest.mark.asyncio
async def test_strong_edge_emits_tagged_signal_and_alert():
    sink = CollectingSink()
    async def discover(): return [_market(0.40)]   # market cheap vs confident leader
    eng = MusicIntelEngine([_Src(_field())], discover, alert_sink=sink)
    res = await eng.run_once("hot100", as_of=D)
    assert res.snapshot_count == 10 and res.market_count == 1
    assert len(res.signals) == 1
    sig = res.signals[0]
    assert sig.source == "chart-intel" and sig.side == "YES"
    assert len(sink.alerts) == 1
    assert "manual execution" in sink.alerts[0]["body"]


@pytest.mark.asyncio
async def test_no_market_is_first_class_no_error():
    sink = CollectingSink()
    async def discover(): return []
    eng = MusicIntelEngine([_Src(_field())], discover, alert_sink=sink)
    res = await eng.run_once("hot100", as_of=D)
    assert res.market_count == 0 and res.signals == []
    assert res.snapshot_count == 10            # projection inputs still ingested
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_no_edge_when_market_fairly_priced():
    sink = CollectingSink()
    async def discover(): return [_market(0.97)]   # market already ~ certain
    eng = MusicIntelEngine([_Src(_field())], discover, alert_sink=sink)
    res = await eng.run_once("hot100", as_of=D)
    assert res.signals == [] and sink.alerts == []


@pytest.mark.asyncio
async def test_trust_hierarchy_prefers_higher_tier():
    low = _Src([_rec("LOW", "x", 1)], tier=1, name="kworb")
    high = _Src([_rec("HIGH", "y", 9)], tier=3, name="luminate")
    async def discover(): return []
    eng = MusicIntelEngine([low, high], discover)
    recs = await eng._ingest("hot100", D)
    assert recs[0].artist == "HIGH"            # tier-3 source wins


@pytest.mark.asyncio
async def test_execution_never_enabled_even_with_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CHART_EXECUTION", "true")
    assert MusicIntelEngine.execution_enabled() is False   # policy: never trades


def test_match_target_finds_artist():
    recs = _field()
    t = _match_target("Will A be number one?", recs)
    assert t is not None and t.artist == "A"


def test_chart_signal_to_market_signal_is_tagged():
    sig = ChartSignal(source="chart-intel", market_id="pm:1", question="Q", chart="hot100",
                      target="A - Song", model_prob=0.8, market_prob=0.4, confidence=0.7,
                      net_edge=0.37, side="YES", drivers=[], note="strong")
    ms = sig.to_market_signal()
    assert ms.reasoning.startswith("[chart-intel]")
    assert ms.ai_probability == 0.8 and ms.direction == "bullish"


# ── REGRESSION: market names a SPECIFIC track, not just the artist ───────────

@pytest.mark.asyncio
async def test_named_track_absent_from_chart_no_false_edge():
    # Live bug: market names "Drop Dead - Olivia Rodrigo" (NOT charting), priced ~0.
    # The engine used to match the ARTIST and project her best-charting song
    # ("stupid song") -> a huge bogus YES edge. The fix projects the NAMED track;
    # absent from the chart -> ~0 prob / low confidence -> NO signal.
    recs = [_rec("Ella Langley", "Choosin' Texas", 11_000_000),
            _rec("Olivia Rodrigo", "stupid song", 11_000_000),   # different song!
            _rec("Drake", "Janice", 7_000_000)] + \
           [_rec(f"D{i}", "x", 1_000_000) for i in range(7)]
    sink = CollectingSink()
    async def discover():
        return [_market(0.0015,
                        q='Will "Drop Dead - Olivia Rodrigo" be the Billboard Hot 100 #1 song?')]
    eng = MusicIntelEngine([_Src(recs)], discover, alert_sink=sink)
    res = await eng.run_once("hot100", as_of=D)
    assert res.signals == []      # no spurious edge from the wrong song
    assert sink.alerts == []


@pytest.mark.asyncio
async def test_named_track_that_leads_chart_still_edges():
    # Positive control: the market names the actual chart leader, cheaply priced
    # -> a genuine YES edge must still fire (the fix doesn't kill real signals).
    recs = [_rec("Ella Langley", "Choosin' Texas", 30_000_000)] + \
           [_rec(f"D{i}", "x", 500_000) for i in range(9)]
    sink = CollectingSink()
    async def discover():
        return [_market(0.30, q="Will \"Choosin' Texas - Ella Langley\" be the Hot 100 #1 song?")]
    eng = MusicIntelEngine([_Src(recs)], discover, alert_sink=sink)
    res = await eng.run_once("hot100", as_of=D)
    assert len(res.signals) == 1 and res.signals[0].side == "YES"
    assert "Ella Langley" in res.signals[0].target
