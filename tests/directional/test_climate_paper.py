import pytest
from core.directional.strategies.climate_paper import ClimatePaperStrategy
from core.directional.climate.registry import ClimateRegistry
from core.directional.climate.base import ClimateProvider, ParsedClimate, ClimateSignal


class _Prov(ClimateProvider):
    family = "t"

    def match(self, m):
        return ParsedClimate("t", "kalshi:" + m.ticker, m.ticker, "nyc", "2026-07-01", "greater", 99.0, None, "temp")

    async def probability(self, parsed, http, ctx):
        return ClimateSignal(0.02, 0.9, "nws")   # very-low -> longshot NO


class _Cfg:
    enabled = True
    mode = "paper"
    longshot_floor = 0.05
    min_edge = 0.10


@pytest.mark.asyncio
async def test_climate_paper_emits_candidate():
    strat = ClimatePaperStrategy(ClimateRegistry([_Prov()]), _Cfg())
    mkt = type("M", (), {"ticker": "KXHIGHNY-26JUL01-T99", "yes_price": 0.12, "no_price": 0.88})()
    out = await strat.scan([mkt], {"http": None})
    assert len(out) == 1 and out[0].side == "NO" and out[0].strategy == "climate_paper"


@pytest.mark.asyncio
async def test_climate_paper_disabled_returns_empty():
    cfg = _Cfg()
    cfg.enabled = False
    strat = ClimatePaperStrategy(ClimateRegistry([_Prov()]), cfg)
    assert await strat.scan([type("M", (), {"ticker": "X", "yes_price": 0.1})()], {}) == []
