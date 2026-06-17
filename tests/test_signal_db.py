"""Tests for utils.signal_db — append-only SQLite store + accuracy metric."""

import pytest

from intelligence.signal import MarketSignal, SignalSummary
from polymarket_client.models import Opportunity, OpportunityType
from utils.signal_db import SignalDB


@pytest.fixture
def db():
    database = SignalDB(db_path=":memory:")
    yield database
    database.close()


def _signal(market_id="m1", ai_prob=0.8, confidence=0.7, price=0.5):
    return MarketSignal(
        market_id=market_id,
        market_question="Will X happen?",
        current_yes_price=price,
        ai_probability=ai_prob,
        confidence=confidence,
        direction="bullish",
        reasoning="news supports YES",
        news_headlines=["a", "b"],
    )


def _opp(market_id="m1", edge=0.03, summary=None):
    opp = Opportunity(
        opportunity_id="o1",
        opportunity_type=OpportunityType.BUNDLE_LONG,
        market_id=market_id,
        edge=edge,
    )
    opp.signal = summary
    return opp


def test_log_signal_returns_id_and_persists(db):
    sid = db.log_signal(_signal(), platform="polymarket")
    assert isinstance(sid, int) and sid > 0
    row = db._conn.execute("SELECT * FROM signals WHERE id=?", (sid,)).fetchone()
    assert row["market_id"] == "m1"
    assert row["news_count"] == 2
    assert row["platform"] == "polymarket"


def test_log_opportunity_pulls_summary_fields(db):
    summary = SignalSummary(
        signal=_signal(), should_filter=True, should_boost=False,
        adjusted_edge=0.08, reason="AI bearish",
    )
    sid = db.log_signal(summary.signal)
    oid = db.log_opportunity(_opp(summary=summary), signal_id=sid)
    row = db._conn.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["raw_edge"] == 0.03
    assert row["adjusted_edge"] == 0.08
    assert row["was_filtered"] == 1
    assert row["filter_reason"] == "AI bearish"
    assert row["signal_id"] == sid


def test_log_opportunity_without_signal(db):
    oid = db.log_opportunity(_opp(summary=None))
    row = db._conn.execute("SELECT * FROM opportunities WHERE id=?", (oid,)).fetchone()
    assert row["was_filtered"] == 0
    assert row["adjusted_edge"] is None
    assert row["signal_id"] is None


def test_accuracy_empty_when_no_outcomes(db):
    db.log_signal(_signal())
    stats = db.get_signal_accuracy()
    assert stats["total_signals"] == 0
    assert stats["accuracy"] == 0.0


def test_accuracy_counts_correct_predictions(db):
    # Two confident signals predicting YES (ai_prob>=0.5); one resolves YES (correct),
    # one resolves NO (incorrect).
    db.log_signal(_signal(market_id="win", ai_prob=0.8, confidence=0.8))
    db.log_signal(_signal(market_id="lose", ai_prob=0.9, confidence=0.8))
    db.log_outcome("win", resolved_yes=True)
    db.log_outcome("lose", resolved_yes=False)

    stats = db.get_signal_accuracy(min_confidence=0.65)
    assert stats["total_signals"] == 2
    assert stats["correct"] == 1
    assert stats["accuracy"] == 0.5
    assert abs(stats["avg_confidence"] - 0.8) < 1e-9
    assert abs(stats["calibration_error"] - 0.3) < 1e-9  # |0.8 - 0.5|


def test_accuracy_respects_min_confidence(db):
    # Low-confidence signal is excluded from the accuracy computation.
    db.log_signal(_signal(market_id="lowconf", ai_prob=0.9, confidence=0.4))
    db.log_outcome("lowconf", resolved_yes=True)
    stats = db.get_signal_accuracy(min_confidence=0.65)
    assert stats["total_signals"] == 0


def test_append_only_multiple_outcomes_allowed(db):
    # Append-only: logging the same market twice creates two rows (no upsert).
    db.log_outcome("m1", resolved_yes=True)
    db.log_outcome("m1", resolved_yes=False)
    count = db._conn.execute(
        "SELECT COUNT(*) AS c FROM outcomes WHERE market_id='m1'"
    ).fetchone()["c"]
    assert count == 2
