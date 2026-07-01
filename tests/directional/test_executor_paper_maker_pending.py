"""Task 1 (realistic fills): paper maker_longshot orders record as pending, not instant-open.

A paper maker order previously simulated an immediate fill (status="open"). That's
unrealistic: a resting NO BUY limit at post_price is not guaranteed to fill instantly
even in live trading. This test locks in the corrected behavior — paper maker_longshot
orders now start "pending" so a later task can model realistic fills from the real
orderbook. The live path (places a real order, records with an order_id) is unchanged.
"""
import pytest

from core.directional.executor import Executor
from core.directional.models import DirectionalOrder


class FakeStore:
    def __init__(self):
        self.saved = []

    def record_position(self, p):
        self.saved.append(p)
        return 1


class FakeKalshiClient:
    def __init__(self, balance=100.0):
        self._balance = balance
        self.place_calls = []

    async def get_balance(self):
        return self._balance

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return object()


@pytest.mark.asyncio
async def test_paper_maker_longshot_records_pending_not_open():
    """Paper maker_longshot NO order at post_price 0.9 must record status='pending'."""
    store, client = FakeStore(), FakeKalshiClient()
    order = DirectionalOrder(
        market_id="kalshi:KX-ML-PENDING",
        side="NO",
        price=0.9,
        size=5,
        notional=4.5,
        strategy="maker_longshot",
    )

    pos = await Executor(client, store).place(order, mode="paper")

    assert pos is not None
    assert pos.status == "pending"
    assert pos.order_id is None
    assert abs(pos.entry_price - 0.9) < 1e-9
    assert client.place_calls == []
    assert len(store.saved) == 1


@pytest.mark.asyncio
async def test_paper_maker_non_kalshi_records_open_not_pending():
    """C1: a PM.US (pmus:) paper maker order must record 'open', NOT 'pending' — the
    tracker's paper-fill reads the Kalshi orderbook (None for a pmus slug) and would
    otherwise strand it 'unfilled' and never resolve it."""
    store, client = FakeStore(), FakeKalshiClient()
    order = DirectionalOrder(
        market_id="pmus:tc-temp-nychigh-2026-07-01-gte90f",
        side="NO", price=0.9, size=5, notional=4.5, strategy="maker_longshot",
    )
    pos = await Executor(client, store).place(order, mode="paper")
    assert pos is not None
    assert pos.status == "open"      # not "pending" — non-Kalshi keeps instant-open
    assert pos.order_id is None
