from core.directional.climate.registry import ClimateRegistry
from core.directional.climate.base import ClimateProvider, ParsedClimate


class _Stub(ClimateProvider):
    family = "stub"

    def match(self, m):
        return ParsedClimate("stub", "kalshi:X", "X", "nyc", "2026-07-01", "greater", 1.0, None, "temp") if getattr(m, "ticker", "") == "X" else None

    async def probability(self, parsed, http, ctx):
        return None


def test_registry_matches_first_provider():
    reg = ClimateRegistry([_Stub()])
    m = type("M", (), {"ticker": "X"})()
    assert reg.match(m)[1].series == "X"
    assert reg.match(type("M", (), {"ticker": "Y"})()) is None
