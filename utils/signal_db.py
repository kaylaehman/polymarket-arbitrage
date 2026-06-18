"""
Signal Database (SQLite)
========================

Append-only persistence for AI signals, opportunities, and market outcomes.
This is the measurement foundation: by recording every MarketSignal alongside
the eventual market resolution, we can later check whether the AI's probability
estimates are actually calibrated (a prerequisite for Kelly sizing — FEAT-05 —
and the backtester — FEAT-10).

Design:
- Python stdlib ``sqlite3`` only (no new dependencies).
- Append-only: rows are inserted, never updated or deleted.
- All writes are best-effort from the caller's perspective; callers in the live
  loop should still guard with try/except so logging never breaks trading.
"""

import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    market_question TEXT,
    platform TEXT,
    current_yes_price REAL,
    ai_probability REAL,
    confidence REAL,
    direction TEXT,
    reasoning TEXT,
    news_count INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    opportunity_type TEXT,
    raw_edge REAL,
    adjusted_edge REAL,
    was_filtered INTEGER,
    filter_reason TEXT,
    signal_id INTEGER REFERENCES signals(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    resolved_yes INTEGER,
    resolution_date TIMESTAMP,
    logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_market ON outcomes(market_id);
"""


class SignalDB:
    """Append-only SQLite store for signals, opportunities, and outcomes."""

    def __init__(self, db_path: str = "data/signals.db"):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        # check_same_thread=False: the bot logs from asyncio tasks that may run
        # on a different thread than the one that opened the connection.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self) -> None:
        """Create tables and indexes if they don't already exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def log_signal(self, signal, platform: Optional[str] = None) -> int:
        """Persist a MarketSignal. Returns the new row id (for linking opps)."""
        cur = self._conn.execute(
            """
            INSERT INTO signals (
                market_id, market_question, platform, current_yes_price,
                ai_probability, confidence, direction, reasoning, news_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.market_id,
                signal.market_question,
                platform,
                signal.current_yes_price,
                signal.ai_probability,
                signal.confidence,
                signal.direction,
                signal.reasoning,
                len(signal.news_headlines or []),
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_opportunity(self, opp, signal_id: Optional[int] = None) -> int:
        """Persist an Opportunity, pulling filter info from its SignalSummary.

        ``opp.signal`` is a ``SignalSummary | None``; when present we record the
        adjusted edge and whether intelligence would have filtered the arb.
        """
        summary = getattr(opp, "signal", None)
        adjusted_edge = summary.adjusted_edge if summary is not None else None
        was_filtered = int(summary.should_filter) if summary is not None else 0
        filter_reason = summary.reason if summary is not None else None

        cur = self._conn.execute(
            """
            INSERT INTO opportunities (
                market_id, opportunity_type, raw_edge, adjusted_edge,
                was_filtered, filter_reason, signal_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                opp.market_id,
                getattr(opp.opportunity_type, "value", str(opp.opportunity_type)),
                opp.edge,
                adjusted_edge,
                was_filtered,
                filter_reason,
                signal_id,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def log_outcome(
        self,
        market_id: str,
        resolved_yes: bool,
        resolution_date: Optional[datetime] = None,
    ) -> int:
        """Record a market's resolution (YES/NO)."""
        cur = self._conn.execute(
            "INSERT INTO outcomes (market_id, resolved_yes, resolution_date) VALUES (?, ?, ?)",
            (
                market_id,
                int(bool(resolved_yes)),
                resolution_date.isoformat() if resolution_date else None,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_unresolved_market_ids(self) -> list[str]:
        """Distinct market_ids that have signals but no recorded outcome yet.

        The outcome poller uses this to know which markets still need a
        resolution check.
        """
        rows = self._conn.execute(
            """
            SELECT DISTINCT market_id FROM signals
            WHERE market_id NOT IN (SELECT market_id FROM outcomes)
            """
        ).fetchall()
        return [r["market_id"] for r in rows]

    def get_signal_accuracy(
        self,
        min_confidence: float = 0.65,
        lookback_days: int = 30,
    ) -> dict:
        """Measure how well signals predicted resolved outcomes.

        A signal "predicts YES" when ai_probability >= 0.5; it is "correct" when
        that prediction matches the resolved outcome. calibration_error is the
        gap between mean confidence and realized accuracy (0 = perfectly
        calibrated). Only signals with a recorded outcome are counted.
        """
        rows = self._conn.execute(
            """
            SELECT s.confidence AS confidence,
                   s.ai_probability AS ai_probability,
                   o.resolved_yes AS resolved_yes
            FROM signals s
            JOIN outcomes o ON s.market_id = o.market_id
            WHERE s.confidence >= ?
              AND s.created_at >= datetime('now', ?)
            """,
            (min_confidence, f"-{int(lookback_days)} days"),
        ).fetchall()

        total = len(rows)
        if total == 0:
            return {
                "total_signals": 0,
                "correct": 0,
                "accuracy": 0.0,
                "avg_confidence": 0.0,
                "calibration_error": 0.0,
            }

        correct = sum(
            1 for r in rows
            if (r["ai_probability"] >= 0.5) == bool(r["resolved_yes"])
        )
        accuracy = correct / total
        avg_conf = sum(r["confidence"] for r in rows) / total
        return {
            "total_signals": total,
            "correct": correct,
            "accuracy": accuracy,
            "avg_confidence": avg_conf,
            "calibration_error": abs(avg_conf - accuracy),
        }

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
