"""
music_intel.store — SQLite persistence for the music intelligence module.

Mirrors the pattern in core/directional/store.py:
  - sqlite3 only (no ORM)
  - Row factory on the connection
  - init_schema() to create tables
  - record_* / get_* / save_* / recent_* methods; reads never raise

Tables
------
chart_snapshots   — one row per ChartRecord; unique on (source, chart, as_of, rank)
projections       — JSON-blob projections keyed by (chart, as_of, market_key)
calibration_curves — JSON-blob calibration data keyed by model_version (upsert)
market_matches    — resolved market vs model probability comparisons
"""

import datetime
import logging
import os
import sqlite3
from typing import Optional

from music_intel.sources.base import ChartRecord

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chart_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    chart            TEXT    NOT NULL,
    as_of            TEXT    NOT NULL,
    rank             INTEGER NOT NULL,
    title            TEXT    NOT NULL,
    artist           TEXT    NOT NULL,
    track_id         TEXT,
    rank_delta       INTEGER,
    streams_period   INTEGER,
    streams_7day     INTEGER,
    days_on_chart    INTEGER,
    peak             INTEGER,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source, chart, as_of, rank)
);

CREATE TABLE IF NOT EXISTS projections (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    market_key     TEXT    NOT NULL,
    chart          TEXT    NOT NULL,
    as_of          TEXT    NOT NULL,
    point_estimate REAL    NOT NULL,
    prob_low       REAL    NOT NULL,
    prob_high      REAL    NOT NULL,
    confidence     REAL    NOT NULL,
    drivers_json   TEXT    NOT NULL,
    model_prob     REAL    NOT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calibration_curves (
    model_version TEXT PRIMARY KEY,
    curve_json    TEXT NOT NULL,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_matches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id    TEXT  NOT NULL,
    question     TEXT  NOT NULL,
    model_prob   REAL  NOT NULL,
    market_prob  REAL  NOT NULL,
    edge         REAL  NOT NULL,
    ts           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_snapshots_lookup
    ON chart_snapshots(source, chart, as_of);
CREATE INDEX IF NOT EXISTS idx_projections_lookup
    ON projections(chart, as_of);
CREATE INDEX IF NOT EXISTS idx_matches_ts
    ON market_matches(ts DESC);
"""


class MusicStore:
    """SQLite store for music-intelligence snapshots, projections, and matches."""

    def __init__(self, db_path: str = "data/music_intel.db"):
        self.db_path = db_path
        if db_path != ":memory:":
            parent = os.path.dirname(db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        """Create all tables and indexes if they do not already exist."""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------
    # chart_snapshots
    # ------------------------------------------------------------------

    def record_snapshot(self, rec: ChartRecord) -> None:
        """Insert a ChartRecord; silently ignores exact duplicates (same unique key)."""
        try:
            self._conn.execute(
                """INSERT OR IGNORE INTO chart_snapshots
                   (source, chart, as_of, rank, title, artist,
                    track_id, rank_delta, streams_period, streams_7day,
                    days_on_chart, peak)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec.source,
                    rec.chart,
                    rec.as_of.isoformat(),
                    rec.rank,
                    rec.title,
                    rec.artist,
                    rec.track_id,
                    rec.rank_delta,
                    rec.streams_period,
                    rec.streams_7day,
                    rec.days_on_chart,
                    rec.peak,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("record_snapshot failed: %s", exc)

    def get_snapshots(
        self, source: str, chart: str, as_of: datetime.date
    ) -> list[ChartRecord]:
        """Return all ChartRecords for the given source/chart/date, ordered by rank."""
        try:
            rows = self._conn.execute(
                """SELECT * FROM chart_snapshots
                   WHERE source = ? AND chart = ? AND as_of = ?
                   ORDER BY rank""",
                (source, chart, as_of.isoformat()),
            ).fetchall()
            return [_row_to_chart_record(r) for r in rows]
        except sqlite3.Error as exc:
            logger.warning("get_snapshots failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # projections
    # ------------------------------------------------------------------

    def record_projection(
        self,
        market_key: str,
        chart: str,
        as_of: datetime.date,
        point_estimate: float,
        prob_low: float,
        prob_high: float,
        confidence: float,
        drivers_json: str,
        model_prob: float,
    ) -> None:
        """Persist a projection blob."""
        try:
            self._conn.execute(
                """INSERT INTO projections
                   (market_key, chart, as_of, point_estimate, prob_low,
                    prob_high, confidence, drivers_json, model_prob)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market_key,
                    chart,
                    as_of.isoformat(),
                    point_estimate,
                    prob_low,
                    prob_high,
                    confidence,
                    drivers_json,
                    model_prob,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("record_projection failed: %s", exc)

    def get_projections(
        self, chart: str, as_of: datetime.date
    ) -> list[dict]:
        """Return all projections for chart/date as plain dicts."""
        try:
            rows = self._conn.execute(
                """SELECT * FROM projections
                   WHERE chart = ? AND as_of = ?
                   ORDER BY id""",
                (chart, as_of.isoformat()),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.warning("get_projections failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # calibration_curves
    # ------------------------------------------------------------------

    def save_calibration(self, version: str, curve_json: str) -> None:
        """Upsert a calibration curve (INSERT OR REPLACE on model_version PK)."""
        try:
            self._conn.execute(
                """INSERT OR REPLACE INTO calibration_curves
                   (model_version, curve_json, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (version, curve_json),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("save_calibration failed: %s", exc)

    def get_calibration(self, version: str) -> Optional[str]:
        """Return the curve JSON for *version*, or None if not found."""
        try:
            row = self._conn.execute(
                "SELECT curve_json FROM calibration_curves WHERE model_version = ?",
                (version,),
            ).fetchone()
            return row["curve_json"] if row else None
        except sqlite3.Error as exc:
            logger.warning("get_calibration failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # market_matches
    # ------------------------------------------------------------------

    def record_match(
        self,
        market_id: str,
        question: str,
        model_prob: float,
        market_prob: float,
        edge: float,
    ) -> None:
        """Record a model-vs-market probability comparison."""
        try:
            self._conn.execute(
                """INSERT INTO market_matches
                   (market_id, question, model_prob, market_prob, edge)
                   VALUES (?, ?, ?, ?, ?)""",
                (market_id, question, model_prob, market_prob, edge),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            logger.warning("record_match failed: %s", exc)

    def recent_matches(self, limit: int = 50) -> list[dict]:
        """Return the most recent *limit* market matches as plain dicts."""
        try:
            rows = self._conn.execute(
                "SELECT * FROM market_matches ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.Error as exc:
            logger.warning("recent_matches failed: %s", exc)
            return []


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _row_to_chart_record(row: sqlite3.Row) -> ChartRecord:
    """Convert a chart_snapshots DB row to a ChartRecord."""
    return ChartRecord(
        source=row["source"],
        chart=row["chart"],
        as_of=datetime.date.fromisoformat(row["as_of"]),
        rank=row["rank"],
        title=row["title"],
        artist=row["artist"],
        track_id=row["track_id"],
        rank_delta=row["rank_delta"],
        streams_period=row["streams_period"],
        streams_7day=row["streams_7day"],
        days_on_chart=row["days_on_chart"],
        peak=row["peak"],
    )
