"""
core/worldcup/ledger.py — SQLite ledger for paper bets.

Records every paper bet placed by worldcup_value_run.py, tracks open/resolved
positions, and computes running PnL.

EXPERIMENTAL / PAPER only.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.worldcup.config import DB_PATH

# Resolve DB_PATH relative to repo root (parent of the scripts/ directory)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_db_path() -> Path:
    return _REPO_ROOT / DB_PATH


@dataclass
class BetRecord:
    id: int
    slug: str
    outcome_type: str
    team_slug: str
    model_prob: float
    market_price: float
    edge: float
    stake: float
    placed_at: float      # unix timestamp
    status: str           # "open" | "won" | "lost" | "void"
    resolved_at: Optional[float]
    pnl: Optional[float]


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS paper_bets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT    NOT NULL,
    outcome_type TEXT    NOT NULL,
    team_slug    TEXT    NOT NULL,
    model_prob   REAL    NOT NULL,
    market_price REAL    NOT NULL,
    edge         REAL    NOT NULL,
    stake        REAL    NOT NULL,
    placed_at    REAL    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'open',
    resolved_at  REAL,
    pnl          REAL
);
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_paper_bets_slug ON paper_bets (slug);
"""


class Ledger:
    """Thread-safe SQLite ledger for WC2026 paper bets."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._path = db_path or _default_db_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self._path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_INDEX)

    def record_bet(
        self,
        slug: str,
        outcome_type: str,
        team_slug: str,
        model_prob: float,
        market_price: float,
        edge: float,
        stake: float,
    ) -> int:
        """Insert a new open paper bet. Returns the new row id."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO paper_bets
                    (slug, outcome_type, team_slug, model_prob,
                     market_price, edge, stake, placed_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (slug, outcome_type, team_slug, model_prob,
                 market_price, edge, stake, time.time()),
            )
            return cur.lastrowid

    def resolve_bet(self, bet_id: int, won: bool) -> None:
        """Mark a paper bet as won or lost and record PnL."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT stake, market_price FROM paper_bets WHERE id = ?", (bet_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No paper bet with id={bet_id}")
            stake = row["stake"]
            price = row["market_price"]
            if won:
                pnl = stake * (1.0 / price - 1.0)
                status = "won"
            else:
                pnl = -stake
                status = "lost"
            conn.execute(
                """
                UPDATE paper_bets
                SET status = ?, resolved_at = ?, pnl = ?
                WHERE id = ?
                """,
                (status, time.time(), round(pnl, 4), bet_id),
            )

    def void_bet(self, bet_id: int) -> None:
        """Mark a bet as void (market cancelled / resolved N/A)."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE paper_bets SET status = 'void', resolved_at = ? WHERE id = ?",
                (time.time(), bet_id),
            )

    def get_open_bets(self) -> list[BetRecord]:
        """Return all open (unresolved) bets."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets WHERE status = 'open' ORDER BY placed_at"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def get_all_bets(self) -> list[BetRecord]:
        """Return all bets."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM paper_bets ORDER BY placed_at"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def summary(self) -> dict:
        """Return aggregate PnL summary."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*)                        AS total_bets,
                    SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_bets,
                    SUM(CASE WHEN status='won'  THEN 1 ELSE 0 END) AS won_bets,
                    SUM(CASE WHEN status='lost' THEN 1 ELSE 0 END) AS lost_bets,
                    SUM(CASE WHEN status='void' THEN 1 ELSE 0 END) AS void_bets,
                    SUM(stake)                      AS total_staked,
                    SUM(COALESCE(pnl, 0))           AS total_pnl
                FROM paper_bets
                """
            ).fetchone()
        return dict(row)


def _row_to_record(row: sqlite3.Row) -> BetRecord:
    return BetRecord(
        id=row["id"],
        slug=row["slug"],
        outcome_type=row["outcome_type"],
        team_slug=row["team_slug"],
        model_prob=row["model_prob"],
        market_price=row["market_price"],
        edge=row["edge"],
        stake=row["stake"],
        placed_at=row["placed_at"],
        status=row["status"],
        resolved_at=row["resolved_at"],
        pnl=row["pnl"],
    )
