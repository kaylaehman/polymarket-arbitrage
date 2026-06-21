"""Tests for core/directional/executor.py — Task 12."""
import pytest
from core.directional.executor import Executor
from core.directional.models import DirectionalOrder


class Store:
    def __init__(self):
        self.saved = []

    def record_position(self, p):
        self.saved.append(p)
        return 1


class Client:
    def __init__(self, balance=100.0):
        self._balance = balance
        self.calls = 0

    async def get_balance(self):
        return self._balance

    async def place_order(self, **k):
        self.calls += 1
        return object()


@pytest.mark.asyncio
async def test_paper_records_no_api():
    st, cl = Store(), Client()
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"),
        mode="paper",
    )
    assert cl.calls == 0
    assert len(st.saved) == 1
    assert st.saved[0].mode == "paper"


@pytest.mark.asyncio
async def test_live_calls_place_order():
    st, cl = Store(), Client(balance=100.0)
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"),
        mode="live",
    )
    assert cl.calls == 1
    assert len(st.saved) == 1
    assert st.saved[0].mode == "live"


@pytest.mark.asyncio
async def test_live_aborts_on_insufficient_balance():
    st, cl = Store(), Client(balance=1.0)  # balance 1.0 < notional 4.5
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.9, 5, 4.5, "safe_compounder"),
        mode="live",
    )
    assert pos is None
    assert cl.calls == 0


@pytest.mark.asyncio
async def test_executor_live_no_order_id_does_not_record_pending():
    """I2 fix: if place_order returns no usable order_id, executor logs error and returns None.

    An unmanaged pending position (no order_id) would leak on the exchange and count
    against exposure forever. The executor must not record it.
    """
    class NullOrderIdClient:
        async def get_balance(self):
            return 100.0

        async def place_order(self, **kwargs):
            o = type("Order", (), {"order_id": None})()
            return o

    st, cl = Store(), NullOrderIdClient()
    pos = await Executor(cl, st).place(
        DirectionalOrder("kalshi:KX-1", "NO", 0.93, 5, 4.65, "maker_longshot"),
        mode="live",
    )
    assert pos is None, "must return None when order_id is absent"
    assert len(st.saved) == 0, "must not record a position without order_id"
