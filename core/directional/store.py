"""
Directional Store
=================

SQLite persistence for directional positions and signals.

This is a SEPARATE store from utils/signal_db.py — it never writes to the live
arb signal tables and uses its own db file (config.directional.db_path).
"""

import logging
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

# Minimum resolved trades before a category's edge gets a statistical verdict.
# Below this the verdict is "insufficient" no matter how good the early run looks
# (small samples of a 90%-win longshot edge are dominated by not-yet-seen tail
# losses).  90% one-sided z for the EV confidence bound.
VALIDATION_MIN_SAMPLE = 30
_Z90 = 1.2815515594457913

from core.directional.models import DirectionalCandidate, DirectionalPosition

logger = logging.getLogger(__name__)


# Ticker-prefix → category map for the per-category validation breakout (#1).
# Order matters: the first matching prefix wins, so longer/more-specific
# prefixes (KXCPICORE) must precede shorter ones (KXCPI) — though here the
# coarse buckets collapse them to the same category anyway.
_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("KXHIGH", "weather"),
    ("KXCPI", "macro"),
    ("KXPCE", "macro"),
    ("KXGDP", "macro"),
    ("KXFED", "macro"),
    ("KXBTC", "financial"),
    ("KXETH", "financial"),
    ("KXWTI", "financial"),
    ("KXEUR", "financial"),
    ("KXCABLE", "media"),
    ("KXNBA", "sports"),
    ("KXNHL", "sports"),
    ("KXMLB", "sports"),
    ("KXNFL", "sports"),
)


def _next_day_iso(day_iso: str) -> str:
    """Return the calendar day after ``day_iso`` ('YYYY-MM-DD'), for a half-open
    ``[day, next_day)`` range over ISO-8601 timestamp strings. Any 'YYYY-MM-DD...'
    value in that window sorts lexicographically below next_day's 'YYYY-MM-DD'."""
    d = datetime.strptime(day_iso[:10], "%Y-%m-%d").date()
    return (d + timedelta(days=1)).isoformat()


def _verdict(closed_pnls: list) -> dict:
    """Statistical go/no-go verdict for a category's resolved P&L (#3).

    Returns ``verdict`` plus a 90% confidence band on mean EV-per-trade:
      * ``"insufficient"`` — fewer than ``VALIDATION_MIN_SAMPLE`` resolved trades
        (with ``needed_samples`` = how many more); never trust a small sample of
        a longshot edge, where rare tail losses dominate.
      * ``"positive"``     — 90% lower bound on mean EV > 0 (edge looks real).
      * ``"negative"``     — 90% upper bound < 0.
      * ``"inconclusive"`` — band straddles 0 (need a tighter sample).

    Band: ``mean ± z90 * stderr`` with the sample standard deviation.  Rough
    (normal approx, ignores P&L skew) but enough to stop "3 wins = ship it".
    """
    n = len(closed_pnls)
    out = {"verdict": "insufficient", "needed_samples": VALIDATION_MIN_SAMPLE,
           "ev_ci90_lo": None, "ev_ci90_hi": None}
    if n == 0:
        return out
    if n < VALIDATION_MIN_SAMPLE:
        out["needed_samples"] = VALIDATION_MIN_SAMPLE - n
        return out

    out["needed_samples"] = 0
    mean = sum(closed_pnls) / n
    var = sum((x - mean) ** 2 for x in closed_pnls) / (n - 1) if n > 1 else 0.0
    stderr = math.sqrt(var / n)
    lo = mean - _Z90 * stderr
    hi = mean + _Z90 * stderr
    out["ev_ci90_lo"] = round(lo, 4)
    out["ev_ci90_hi"] = round(hi, 4)
    if lo > 0:
        out["verdict"] = "positive"
    elif hi < 0:
        out["verdict"] = "negative"
    else:
        out["verdict"] = "inconclusive"
    return out


def category_for_market_id(market_id: str) -> str:
    """Map a venue-prefixed market_id to a coarse trading category.

    Used by the per-category validation breakout so win-rate / EV can be judged
    per category (weather vs macro vs ...) instead of in one weather-dominated
    aggregate.  PM.US temperature slugs (``pmus:tc-temp-...``) are weather;
    bare/unknown tickers fall through to ``"other"``.
    """
    if market_id.startswith("pmus:"):
        slug = market_id.split(":", 1)[1]
        if "temp" in slug or "high" in slug or "low" in slug:
            return "weather"
        return "other"
    if market_id.startswith("pm:"):
        return "music"
    ticker = market_id.split(":", 1)[1] if ":" in market_id else market_id
    ticker = ticker.upper()
    for prefix, category in _CATEGORY_PREFIXES:
        if ticker.startswith(prefix):
            return category
    return "other"


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
    order_id TEXT,
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

CREATE TABLE IF NOT EXISTS climate_calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT, strategy TEXT, p_yes REAL, outcome_yes INTEGER,
    recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
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
                stop_loss, take_profit, notional, status, order_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                position.order_id,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def record_calibration(self, market_id, strategy, p_yes, outcome_yes) -> None:
        """Log a predicted-vs-actual outcome pair for later reliability analysis."""
        self._conn.execute(
            "INSERT INTO climate_calibration(market_id,strategy,p_yes,outcome_yes) VALUES(?,?,?,?)",
            (market_id, strategy, float(p_yes), int(outcome_yes)),
        )
        self._conn.commit()

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

    def pending_positions(self) -> list:
        """Return all pending maker DirectionalPosition objects (awaiting fill)."""
        rows = self._conn.execute(
            "SELECT * FROM directional_positions WHERE status = 'pending' ORDER BY id"
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def recent_signals(self, limit: int = 50) -> list:
        """Return the most recent signal dicts."""
        rows = self._conn.execute(
            "SELECT * FROM directional_signals ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def directional_exposure(self) -> float:
        """Sum of notional across all open and pending positions."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(notional), 0.0) FROM directional_positions WHERE status IN ('open', 'pending')"
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

    def strategies(self) -> list:
        """Distinct strategy names across ALL positions (open and closed), sorted.

        Used by the dashboard so a strategy with only closed positions (e.g. an arb
        strategy that has realized P&L but no open exposure) is still listed.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT strategy FROM directional_positions ORDER BY strategy"
        ).fetchall()
        return [row["strategy"] for row in rows]

    def pnl_summary_by_mode(self) -> dict:
        """Per-mode P&L summary: {mode: {open_count, closed_count, open_exposure,
        total_realized_pnl}}.

        Lets the dashboard show paper vs actual (live) separately. Only modes that
        have at least one position appear — an empty store returns {}.
        """
        rows = self._conn.execute(
            """SELECT mode,
                COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                COUNT(*) FILTER (WHERE status = 'closed') AS closed_count,
                COALESCE(SUM(notional) FILTER (WHERE status = 'open'), 0.0) AS open_exposure,
                COALESCE(SUM(realized_pnl) FILTER (WHERE status = 'closed'), 0.0) AS total_realized_pnl
               FROM directional_positions
               GROUP BY mode"""
        ).fetchall()
        # Directional win-rate per mode: wins / closed over NON-riskless strategies.
        # Riskless-arb strategies (multi_outcome, cross_platform_arb, bundle_arb) buy
        # every leg of an event, so their per-leg "losses" are by design and would
        # drag a directional win-rate down misleadingly — exclude them.
        from core.directional.validation import RISKLESS_STRATEGIES
        placeholders = ",".join("?" for _ in RISKLESS_STRATEGIES) or "''"
        wr = {
            r["mode"]: (r["wins"], r["dir_closed"])
            for r in self._conn.execute(
                f"""SELECT mode,
                    COUNT(*) FILTER (WHERE realized_pnl > 0) AS wins,
                    COUNT(*) AS dir_closed
                   FROM directional_positions
                   WHERE status = 'closed' AND strategy NOT IN ({placeholders})
                   GROUP BY mode""",
                tuple(RISKLESS_STRATEGIES),
            ).fetchall()
        }
        out = {}
        for row in rows:
            wins, dir_closed = wr.get(row["mode"], (0, 0))
            out[row["mode"]] = {
                "open_count": row["open_count"],
                "closed_count": row["closed_count"],
                "open_exposure": float(row["open_exposure"]),
                "total_realized_pnl": float(row["total_realized_pnl"]),
                "wins": wins,
                "dir_closed": dir_closed,
                "win_rate": (wins / dir_closed) if dir_closed else None,
            }
        return out

    def daily_pnl_by_mode(self, day_iso: str) -> dict:
        """Per-mode P&L for positions that RESOLVED (closed) on ``day_iso`` (a
        ``YYYY-MM-DD`` UTC date) and bets that were PLACED (opened) that day.

        Returns ``{mode: {settled_count, wins, losses, realized_pnl, opened_count}}``.
        ``closed_at``/``opened_at`` are ISO-8601 UTC strings, so a ``>= day``/
        ``< day+1`` half-open string range selects the calendar day correctly.
        A resolved position with NULL ``realized_pnl`` (an unfilled/TTL-cancelled
        maker order) counts toward neither wins nor losses and contributes $0.

        Only modes with activity that day appear; a quiet day returns ``{}``.
        """
        lo = day_iso
        hi = _next_day_iso(day_iso)
        out: dict = {}
        # Settled today, grouped by mode.
        for r in self._conn.execute(
            """SELECT mode,
                COUNT(*) AS settled_count,
                COUNT(*) FILTER (WHERE realized_pnl > 0) AS wins,
                COUNT(*) FILTER (WHERE realized_pnl < 0) AS losses,
                COALESCE(SUM(realized_pnl), 0.0) AS realized_pnl
               FROM directional_positions
               WHERE status = 'closed' AND closed_at >= ? AND closed_at < ?
               GROUP BY mode""",
            (lo, hi),
        ).fetchall():
            out[r["mode"]] = {
                "settled_count": r["settled_count"],
                "wins": r["wins"],
                "losses": r["losses"],
                "realized_pnl": float(r["realized_pnl"]),
                "opened_count": 0,
            }
        # Bets placed today, grouped by mode (merged into the same buckets).
        for r in self._conn.execute(
            """SELECT mode, COUNT(*) AS opened_count
               FROM directional_positions
               WHERE opened_at >= ? AND opened_at < ?
               GROUP BY mode""",
            (lo, hi),
        ).fetchall():
            bucket = out.setdefault(
                r["mode"],
                {"settled_count": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0, "opened_count": 0},
            )
            bucket["opened_count"] = r["opened_count"]
        return out

    def category_breakdown(self) -> dict:
        """Per-category validation breakout (#1).

        Buckets every position by ``category_for_market_id(market_id)`` and, for
        each category, reports the metrics needed to decide whether the
        longshot-NO edge actually holds *for that category* (vs. being a
        weather-only artifact):

          * ``closed_count`` / ``wins`` / ``losses`` — resolved-trade sample.
          * ``win_rate`` — wins / closed, or ``None`` when nothing has resolved.
          * ``realized_pnl`` — net realized P&L (the tracker records P&L gross of
            Kalshi fees; treat as an upper bound on live EV).
          * ``avg_pnl_per_trade`` — realized_pnl / closed_count, or ``None``.
          * ``open_count`` / ``open_exposure`` — current live paper book.

        A ``win`` is a resolved position with ``realized_pnl > 0``.  Pending
        positions (never filled) are ignored — only ``open`` and ``closed``
        count.
        """
        # Exclude multi_outcome locks: they are riskless N-leg YES covers (exactly
        # one leg wins) whose 1/N "win rate" would muddy the longshot-NO edge
        # signal this breakout exists to measure.  They get their own summary.
        rows = self._conn.execute(
            """SELECT market_id, status, notional, realized_pnl
               FROM directional_positions
               WHERE status IN ('open', 'closed')
                 AND strategy != 'multi_outcome'"""
        ).fetchall()

        agg: dict[str, dict] = {}
        pnls: dict[str, list] = {}
        for r in rows:
            cat = category_for_market_id(r["market_id"])
            b = agg.setdefault(
                cat,
                {"closed_count": 0, "wins": 0, "losses": 0,
                 "realized_pnl": 0.0, "open_count": 0, "open_exposure": 0.0},
            )
            if r["status"] == "open":
                b["open_count"] += 1
                b["open_exposure"] += float(r["notional"] or 0.0)
            elif r["status"] == "closed":
                b["closed_count"] += 1
                pnl = float(r["realized_pnl"] or 0.0)
                b["realized_pnl"] += pnl
                pnls.setdefault(cat, []).append(pnl)
                if pnl > 0:
                    b["wins"] += 1
                elif pnl < 0:
                    b["losses"] += 1

        for cat, b in agg.items():
            closed = b["closed_count"]
            b["win_rate"] = (b["wins"] / closed) if closed else None
            b["avg_pnl_per_trade"] = (b["realized_pnl"] / closed) if closed else None
            b["realized_pnl"] = round(b["realized_pnl"], 4)
            b["open_exposure"] = round(b["open_exposure"], 4)
            b.update(_verdict(pnls.get(cat, [])))
        return agg

    def multi_outcome_summary(self) -> dict:
        """Aggregate state of riskless multi-outcome lock legs (strategy='multi_outcome').

        Each lock is recorded as N YES legs; when the event resolves exactly one
        settles to +1 and the rest to 0, so the net realized P&L across a lock's
        legs is the (paper, fee-gross) riskless profit.
        """
        row = self._conn.execute(
            """SELECT
                COUNT(*) FILTER (WHERE status = 'open') AS open_count,
                COUNT(*) FILTER (WHERE status = 'closed') AS closed_count,
                COALESCE(SUM(realized_pnl) FILTER (WHERE status = 'closed'), 0.0) AS realized_pnl,
                COALESCE(SUM(notional) FILTER (WHERE status = 'open'), 0.0) AS open_notional
               FROM directional_positions
               WHERE strategy = 'multi_outcome'"""
        ).fetchone()
        if row is None:
            return {"open_count": 0, "closed_count": 0, "realized_pnl": 0.0, "open_notional": 0.0}
        return {
            "open_count": row["open_count"],
            "closed_count": row["closed_count"],
            "realized_pnl": round(float(row["realized_pnl"]), 4),
            "open_notional": round(float(row["open_notional"]), 4),
        }

    def maker_fill_stats(self, strategy: str = "maker_longshot") -> dict:
        """Fill-rate report for maker positions of a given strategy.

        Returns a dict with:
          - pending: count of resting (never filled) positions
          - filled_open: count of filled, currently-held positions (status='open')
          - filled_settled: count of filled and resolved positions (status='closed')
          - unfilled: count of positions that never filled (status='unfilled')
          - fill_rate: (filled_open + filled_settled) / (filled_open + filled_settled + unfilled),
                      or None if denominator is 0
          - win_rate_filled: wins / filled_settled among closed positions,
                            or None if filled_settled is 0
                            (win = realized_pnl > 0)
        """
        row = self._conn.execute(
            """SELECT
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'open') AS filled_open,
                COUNT(*) FILTER (WHERE status = 'closed') AS filled_settled,
                COUNT(*) FILTER (WHERE status = 'unfilled') AS unfilled,
                COUNT(*) FILTER (WHERE status = 'closed' AND realized_pnl > 0) AS wins
               FROM directional_positions
               WHERE strategy = ?""",
            (strategy,),
        ).fetchone()

        if row is None:
            return {
                "pending": 0,
                "filled_open": 0,
                "filled_settled": 0,
                "unfilled": 0,
                "fill_rate": None,
                "win_rate_filled": None,
            }

        pending = row["pending"]
        filled_open = row["filled_open"]
        filled_settled = row["filled_settled"]
        unfilled = row["unfilled"]
        wins = row["wins"]

        # fill_rate = (open + closed) / (open + closed + unfilled)
        filled = filled_open + filled_settled
        denominator = filled + unfilled
        fill_rate = (filled / denominator) if denominator > 0 else None

        # win_rate_filled = wins / filled_settled
        win_rate_filled = (wins / filled_settled) if filled_settled > 0 else None

        return {
            "pending": pending,
            "filled_open": filled_open,
            "filled_settled": filled_settled,
            "unfilled": unfilled,
            "fill_rate": fill_rate,
            "win_rate_filled": win_rate_filled,
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
        order_id=row["order_id"] if "order_id" in row.keys() else None,
    )
