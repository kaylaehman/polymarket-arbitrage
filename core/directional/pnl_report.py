"""Discord-ready P&L report builders for the ``/trade-report`` and
``/daily-report`` ClawdBot commands.

Pure formatting over the DirectionalStore — no I/O, no trading. Each function
returns a single Discord-flavored markdown string (bold headers, emojis, a
fenced code block for aligned numbers) that the Discord bridge posts verbatim.

Modes: the paper engine records ``mode='paper'``; a real live account (if ever
enabled) records ``mode='live'`` and the dashboard also synthesizes a ``'live'``
bucket from the broker. These reports show whatever modes have activity — today
that's paper only.
"""
from __future__ import annotations

import datetime
from typing import Any


def _cue(x: float) -> str:
    """🟢 for non-negative, 🔴 for negative."""
    return "🟢" if x >= 0 else "🔴"


def _pct(wins: int, closed: int) -> str:
    return f"{wins / closed * 100:.0f}%" if closed else "—"


def today_utc() -> str:
    """Current UTC calendar day as 'YYYY-MM-DD' (injectable point for tests)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def total_report(store: Any) -> str:
    """Lifetime P&L summary across all modes (the ``/trade-report`` command).

    Uses ``store.pnl_summary_by_mode()``: open/closed counts, open exposure,
    realized P&L, and the directional win-rate (riskless-arb legs excluded).
    """
    by_mode = store.pnl_summary_by_mode() or {}
    out = ["📊 **Trade Report — lifetime P&L**"]
    if not by_mode:
        out.append("_No positions recorded yet._")
        return "\n".join(out)

    # Deterministic order: paper, live, then anything else alphabetically.
    order = {"paper": 0, "live": 1}
    modes = sorted(by_mode, key=lambda m: (order.get(m, 2), m))

    block = []
    grand_realized = 0.0
    for mode in modes:
        b = by_mode[mode]
        realized = float(b.get("total_realized_pnl", 0.0))
        grand_realized += realized
        wins, dir_closed = b.get("wins", 0), b.get("dir_closed", 0)
        block.append(
            f"{mode:<6} realized {_cue(realized)} ${realized:+.2f}  "
            f"open {b.get('open_count', 0):>3} (${float(b.get('open_exposure', 0.0)):.2f})  "
            f"closed {b.get('closed_count', 0):>3}  win {_pct(wins, dir_closed)}"
        )
    out.append("```\n" + "\n".join(block) + "\n```")
    if len(modes) > 1:
        out.append(f"{_cue(grand_realized)} **Total realized:** ${grand_realized:+.2f}")
    return "\n".join(out)


def daily_report(store: Any, day_iso: str | None = None) -> str:
    """P&L for a single UTC day (the ``/daily-report`` command).

    Shows, per mode: bets placed today, bets that resolved today, today's
    win/loss split, and realized P&L booked today. Uses
    ``store.daily_pnl_by_mode(day)``.
    """
    day_iso = day_iso or today_utc()
    by_mode = store.daily_pnl_by_mode(day_iso) or {}
    out = [f"📅 **Daily Report — {day_iso} (UTC)**"]
    if not by_mode:
        out.append("_No bets placed or resolved today._")
        return "\n".join(out)

    order = {"paper": 0, "live": 1}
    modes = sorted(by_mode, key=lambda m: (order.get(m, 2), m))

    block = []
    grand_realized = 0.0
    for mode in modes:
        b = by_mode[mode]
        realized = float(b.get("realized_pnl", 0.0))
        grand_realized += realized
        settled = b.get("settled_count", 0)
        wins, losses = b.get("wins", 0), b.get("losses", 0)
        block.append(
            f"{mode:<6} placed {b.get('opened_count', 0):>3}  "
            f"settled {settled:>3} ({wins}W/{losses}L)  "
            f"realized {_cue(realized)} ${realized:+.2f}"
        )
    out.append("```\n" + "\n".join(block) + "\n```")
    if len(modes) > 1:
        out.append(f"{_cue(grand_realized)} **Total realized today:** ${grand_realized:+.2f}")
    return "\n".join(out)
