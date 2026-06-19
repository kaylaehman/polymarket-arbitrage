"""Tests for AiDirectional strategy (Task 10)."""
import pytest
from types import SimpleNamespace
from core.directional.strategies.ai_directional import AiDirectional
from kalshi_client.models import KalshiMarket


def mk(ticker, yes_price, category, vol=9000):
    return KalshiMarket(
        ticker=ticker,
        event_ticker=ticker.split("-")[0],
        series_ticker=ticker.split("-")[0],
        title="x",
        yes_price=yes_price,
        category=category,
        volume=vol,
    )


class FakeIntel:
    async def evaluate(self, **k):
        sig = SimpleNamespace(
            ai_probability=0.7,
            confidence=0.85,
            direction="bullish",
            edge_vs_market=0.12,
            reasoning="news",
        )
        return SimpleNamespace(signal=sig)


@pytest.mark.asyncio
async def test_emits_yes_on_strong_bullish():
    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(FakeIntel(), min_confidence=0.60, min_edge_pct=0.05)
    cands = await s.scan([m], ctx={})
    assert len(cands) == 1 and cands[0].side == "YES" and cands[0].confidence == 0.85


@pytest.mark.asyncio
async def test_skips_low_confidence():
    class Weak(FakeIntel):
        async def evaluate(self, **k):
            sig = SimpleNamespace(
                ai_probability=0.5,
                confidence=0.4,
                direction="bullish",
                edge_vs_market=0.2,
                reasoning="",
            )
            return SimpleNamespace(signal=sig)

    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(Weak(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []


@pytest.mark.asyncio
async def test_skips_none_signal():
    class NoneIntel:
        async def evaluate(self, **k):
            return None

    m = mk("KXCPI-1", 0.58, "Finance")
    s = AiDirectional(NoneIntel(), 0.60, 0.05)
    result = await s.scan([m], ctx={})
    assert result == []
