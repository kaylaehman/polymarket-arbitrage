import pytest
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta
from core.directional.strategies.maker_longshot import MakerLongshotStrategy
from core.macro_data import MacroMarket


class _MacroCfg:
    enabled = True
    min_sigma = 2.0
    require_data = True
    horizon_days = 45
    sigma = {"CPIYOY": 0.12}


class _FakeMacro:
    def __init__(self, val):
        self._val = val
    async def nowcast(self, indicator):
        return self._val


def _mk_market():
    m = SimpleNamespace()
    m.ticker = "KXCPIYOY-26JUN-T3.9"
    m.title = "CPI YoY >= 3.9%"
    m.category = "macro"
    m.yes_price = 0.06
    m.close_time = datetime.now(timezone.utc) + timedelta(days=10)
    m.to_unified_market_id = lambda: "kalshi:KXCPIYOY-26JUN-T3.9"
    return m


def _strategy():
    return MakerLongshotStrategy(
        min_structural_score=0.0,
        max_yes_price=1.0,
        price_improvement_cents=1,
        skip_categories=[],
        min_yes_price=0.0,
        max_days_to_resolution=45,
        macro_cfg=_MacroCfg(),
    )


@pytest.mark.asyncio
async def test_macro_gate_keeps_deep_tail():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    ctx = {"macro": _FakeMacro(3.2)}  # nowcast 3.2, thr 3.9, σ0.12 -> z≈5.8 -> KEEP
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, ctx) is True


@pytest.mark.asyncio
async def test_macro_gate_skips_near_threshold():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    ctx = {"macro": _FakeMacro(3.87)}  # z≈0.25 < 2.0 -> SKIP
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, ctx) is False


@pytest.mark.asyncio
async def test_macro_gate_skips_when_no_data_and_require():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    assert await s._apply_macro_gate(_mk_market(), mm, 10.0, {"macro": _FakeMacro(None)}) is False


@pytest.mark.asyncio
async def test_macro_gate_skips_beyond_horizon():
    s = _strategy()
    mm = MacroMarket("KXCPIYOY", "CPIYOY", 3.9, "above", "threshold")
    # delta_days 60 > horizon 45 with require_data -> SKIP regardless of nowcast
    assert await s._apply_macro_gate(_mk_market(), mm, 60.0, {"macro": _FakeMacro(3.2)}) is False
