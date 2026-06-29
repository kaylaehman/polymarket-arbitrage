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
