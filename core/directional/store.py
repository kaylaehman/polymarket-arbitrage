"""
Directional Store
=================

SQLite persistence for directional positions and signals.

This is a SEPARATE store from utils/signal_db.py — it never writes to the live
arb signal tables and uses its own db file (config.directional.db_path).
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from core.directional.models import DirectionalCandidate, DirectionalPosition

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS directional_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    size INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    mode TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    stop_loss REAL,
    take_profit REAL,
    notional REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'open',
    realized_pnl REAL,
    closed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS directional_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    market_price REAL,
    edge REAL,
    confidence REAL,
    ai_probability REAL,
    reasoning TEXT,
    placed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_dirpos_market ON directional_positions(market_id);
CREATE INDEX IF NOT EXISTS idx_dirpos_status ON directional_positions(status);
CREATE INDEX IF NOT EXISTS idx_dirsig_market ON directional_signals(market_id);
"""


class DirectionalStore:
    """SQLite store for directional trading positions and signals."""

    def __init__(self, db_path: str = "data/directional.db"):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """Create tables if they don't exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record_signal(self, candidate: DirectionalCandidate, placed: bool) -> int:
        """Insert a signal record. Returns the new row id."""
        cur = self._conn.execute(
            """INSERT INTO directional_signals
               (market_id, side, strategy, market_price, edge, confidence, ai_probability, reasoning, placed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                candidate.market_id,
                candidate.side,
                candidate.strategy,
                candidate.market_price,
                candidate.edge,
                candidate.confidence,
                candidate.ai_probability,
                candidate.reasoning,
                1 if placed else 0,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def record_position(self, position: DirectionalPosition) -> int:
        """Insert a position record. Returns the new row id."""
        opened_at = (
            position.opened_at.isoformat()
            if isinstance(position.opened_at, datetime)
            else position.opened_at
        )
        cur = self._conn.execute(
            """INSERT INTO directional_positions
               (market_id, side, entry_price, size, strategy, mode, opened_at,
                stop_loss, take_profit, notional, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position.market_id,
                position.side,
                position.entry_price,
                position.size,
                position.strategy,
                position.mode,
                opened_at,
                position.stop_loss,
                position.take_profit,
                position.notional,
                position.status,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def update_position(self, market_id: str, **fields) -> None:
        """Update fields on the most recent position for market_id."""
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [market_id]
        self._conn.execute(
            f"""UPDATE directional_positions SET {set_clause}
                WHERE id = (
                    SELECT id FROM directional_positions
                    WHERE market_id = ?
                    ORDER BY id DESC LIMIT 1
                )""",
            values,
        )
        self._conn.commit()

    def open_positions(self) -> list:
        """Return all open DirectionalPosition objects."""
        rows = self._conn.execute(
            "SELECT * FROM directional_positions WHERE status = 'open' ORDER BY id"
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def recent_signals(self, limit: int = 50) -> list:
        """Return the most recent signal dicts."""
        rows = self._conn.execute(
            "SELECT * FROM directional_signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def directional_exposure(self) -> float:
        """Sum of notional across all open positions."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(notional), 0.0) FROM directional_positions WHERE status = 'open'"
        ).fetchone()
        return float(row[0])

    def pnl_summary(self) -> dict:
        """Aggregate P&L summary."""
        row = self._conn.execute(
            """SELECT
                COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                COUNT(*) FILTER (WHERE status = 'closed') AS closed_count,
                COALESCE(SUM(notional) FILTER (WHERE status = 'open'), 0.0) AS open_exposure,
                COALESCE(SUM(realized_pnl) FILTER (WHERE status = 'closed'), 0.0) AS total_realized_pnl
               FROM directional_positions"""
        ).fetchone()
        if row is None:
            return {"open_count": 0, "closed_count": 0, "open_exposure": 0.0, "total_realized_pnl": 0.0}
        return {
            "open_count": row["open_count"],
            "closed_count": row["closed_count"],
            "open_exposure": float(row["open_exposure"]),
            "total_realized_pnl": float(row["total_realized_pnl"]),
        }


def _row_to_position(row: sqlite3.Row) -> DirectionalPosition:
    """Convert a DB row to a DirectionalPosition."""
    opened_at = row["opened_at"]
    if isinstance(opened_at, str):
        try:
            opened_at = datetime.fromisoformat(opened_at)
        except ValueError:
            opened_at = datetime.now(timezone.utc)
    return DirectionalPosition(
        market_id=row["market_id"],
        side=row["side"],
        entry_price=row["entry_price"],
        size=row["size"],
        strategy=row["strategy"],
        mode=row["mode"],
        opened_at=opened_at,
        stop_loss=row["stop_loss"],
        take_profit=row["take_profit"],
        notional=row["notional"],
        status=row["status"],
    )
