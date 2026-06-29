import pytest
from core.directional.strategies.music_paper import MusicPaperStrategy

class _Sig:
    def __init__(self, side="YES"):
        self.source="chart-intel"; self.market_id="pm:12345"; self.question="Will X be #1?"
        self.target="Artist - Song"; self.model_prob=0.80; self.market_prob=0.30
        self.confidence=0.7; self.net_edge=0.50; self.side=side; self.note="strong"
class _Res:
    def __init__(self, sigs): self.signals=sigs
class _Eng:
    def __init__(self, sigs, exec_on=False): self._sigs=sigs; self._exec=exec_on
    def execution_enabled(self): return self._exec
    async def run_once(self, chart, as_of=None): return _Res(self._sigs)

@pytest.mark.asyncio
async def test_converts_signal_to_music_candidate():
    s = MusicPaperStrategy(engine=_Eng([_Sig("YES")]), charts=["spotify_us_daily"])
    cands = await s.scan([], {"no_ask": lambda t: None})
    assert len(cands) == 1
    c = cands[0]
    assert c.market_id == "pm:12345" and c.category == "music" and c.side == "YES"
    assert c.strategy == "music_paper"
    assert c.market_price == pytest.approx(0.30)   # YES entry = market YES price

@pytest.mark.asyncio
async def test_no_side_entry_price_is_one_minus_yes():
    s = MusicPaperStrategy(engine=_Eng([_Sig("NO")]), charts=["spotify_us_daily"])
    c = (await s.scan([], {}))[0]
    assert c.side == "NO" and c.market_price == pytest.approx(0.70)

@pytest.mark.asyncio
async def test_refuses_when_execution_enabled():
    # hard safety: if the music engine ever reports execution enabled, emit nothing
    s = MusicPaperStrategy(engine=_Eng([_Sig("YES")], exec_on=True), charts=["spotify_us_daily"])
    assert await s.scan([], {}) == []

@pytest.mark.asyncio
async def test_run_once_error_is_swallowed():
    class _BadEng:
        def execution_enabled(self): return False
        async def run_once(self, chart, as_of=None): raise RuntimeError("boom")
    s = MusicPaperStrategy(engine=_BadEng(), charts=["spotify_us_daily"])
    assert await s.scan([], {}) == []

@pytest.mark.asyncio
async def test_throttle_skips_within_interval():
    # engine.run_once should be called only once across two quick scans
    calls = {"n": 0}
    class _Sig:
        market_id="pm:1"; question="q"; target="t"; model_prob=0.8; market_prob=0.3
        confidence=0.7; net_edge=0.5; side="YES"
    class _Res:
        signals=[_Sig()]
    class _Eng:
        def execution_enabled(self): return False
        async def run_once(self, chart, as_of=None):
            calls["n"] += 1; return _Res()
    s = MusicPaperStrategy(engine=_Eng(), charts=["spotify_us_daily"], min_refresh_seconds=9999)
    first = await s.scan([], {})
    second = await s.scan([], {})
    assert len(first) == 1            # first cycle runs
    assert second == []              # second cycle throttled
    assert calls["n"] == 1           # engine hit only once

@pytest.mark.asyncio
async def test_zero_interval_runs_every_time():
    calls = {"n": 0}
    class _Sig:
        market_id="pm:1"; question="q"; target="t"; model_prob=0.8; market_prob=0.3
        confidence=0.7; net_edge=0.5; side="YES"
    class _Res: signals=[_Sig()]
    class _Eng:
        def execution_enabled(self): return False
        async def run_once(self, chart, as_of=None):
            calls["n"] += 1; return _Res()
    s = MusicPaperStrategy(engine=_Eng(), charts=["spotify_us_daily"], min_refresh_seconds=0)
    await s.scan([], {}); await s.scan([], {})
    assert calls["n"] == 2
