"""Tests for the outcome poller, get_unresolved_market_ids, and Gamma parsing (FEAT-09)."""

from datetime import datetime, timezone

import pytest

from intelligence.signal import MarketSignal
from polymarket_client.api import PolymarketClient
from polymarket_client.models import Market
from utils.outcome_poller import OutcomePoller
from utils.signal_db import SignalDB


class _FakeClient:
    """Returns preset Market objects; raises for unknown ids."""

    def __init__(self, markets: dict):
        self._markets = markets

    async def get_market(self, market_id: str) -> Market:
        if market_id not in self._markets:
            raise RuntimeError("not found")
        return self._markets[market_id]


def _signal(market_id):
    return MarketSignal(market_id, "q", 0.5, 0.8, 0.8, "bullish", "r", [])


@pytest.fixture
def db():
    database = SignalDB(":memory:")
    yield database
    database.close()


def test_get_unresolved_market_ids(db):
    db.log_signal(_signal("a"))
    db.log_signal(_signal("b"))
    db.log_outcome("a", resolved_yes=True)
    assert db.get_unresolved_market_ids() == ["b"]


async def test_poll_once_logs_resolved_skips_others(db):
    db.log_signal(_signal("won"))
    db.log_signal(_signal("pending"))
    db.log_signal(_signal("unknown"))  # client will raise -> skipped

    markets = {
        "won": Market(market_id="won", condition_id="c", question="q",
                      resolved=True, resolution="YES",
                      end_date=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "pending": Market(market_id="pending", condition_id="c", question="q",
                          resolved=False, resolution=None),
    }
    poller = OutcomePoller(_FakeClient(markets), db)

    logged = await poller.poll_once()
    assert logged == 1
    # "won" now has an outcome; the others remain unresolved.
    assert set(db.get_unresolved_market_ids()) == {"pending", "unknown"}
    row = db._conn.execute("SELECT resolved_yes FROM outcomes WHERE market_id='won'").fetchone()
    assert row["resolved_yes"] == 1


async def test_poll_once_ignores_resolved_without_clear_outcome(db):
    db.log_signal(_signal("weird"))
    markets = {
        "weird": Market(market_id="weird", condition_id="c", question="q",
                        resolved=True, resolution=None),  # resolved but no YES/NO
    }
    poller = OutcomePoller(_FakeClient(markets), db)
    assert await poller.poll_once() == 0
    assert db.get_unresolved_market_ids() == ["weird"]


def test_parse_resolution_from_outcome_prices():
    assert PolymarketClient._parse_resolution('["1", "0"]') == "YES"
    assert PolymarketClient._parse_resolution('["0", "1"]') == "NO"
    assert PolymarketClient._parse_resolution("0.95,0.05") is None  # not JSON list
    assert PolymarketClient._parse_resolution("") is None
    assert PolymarketClient._parse_resolution("garbage") is None


def test_parse_iso_datetime():
    dt = PolymarketClient._parse_iso_datetime("2026-06-17T12:00:00Z")
    assert dt is not None and dt.year == 2026 and dt.month == 6
    assert PolymarketClient._parse_iso_datetime(None) is None
    assert PolymarketClient._parse_iso_datetime("not-a-date") is None
