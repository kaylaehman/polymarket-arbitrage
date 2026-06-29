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
