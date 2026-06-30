"""
Daily Discord digest for the directional paper-trading bot.

Read-only; never trades. Sends once per day via the existing alert pipeline.
"""
from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from music_intel.sources.markets import _parse_json_list, _GAMMA_DEFAULT

logger = logging.getLogger(__name__)

_KWORB_URL = "https://kworb.net/spotify/artists.html"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_HEALTH_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def position_mtm(entry_price: float, side: str, size: int, current_yes: float) -> float:
    """Unrealized P&L for a paper position given the current YES price.

    YES side: current cost is current_yes.
    NO  side: current cost is 1 - current_yes.
    unrealized = (current_side_price - entry_price) * size
    """
    current_side = current_yes if side.upper() == "YES" else (1.0 - current_yes)
    return round((current_side - entry_price) * size, 4)


def digest_text(
    *,
    open_count: int,
    by_strategy: Dict[str, int],
    mtm_lines: List[str],
    promotion_report: str,
    source_health: Dict[str, bool],
) -> str:
    """Assemble the daily digest as Discord-flavored markdown.

    Pure; no I/O. Discord renders markdown in the message content (the Alerter
    sends ``{"content": text}``), so this uses bold headers, emojis, fenced
    code blocks for aligned tables, and 🟢/🔴 P&L cues.
    """
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out: List[str] = [f"📊 **Daily Paper Digest** — {today}"]

    # Open positions
    strat_str = " · ".join(f"{k} {v}" for k, v in by_strategy.items()) or "none"
    out.append(f"\n📂 **Open positions:** {open_count}  ·  {strat_str}")

    # Mark-to-market (Polymarket) — 🟢/🔴 per line, fenced for alignment, + total
    out.append("\n💰 **Mark-to-market (Polymarket)**")
    if mtm_lines:
        block = []
        for ml in mtm_lines:
            cue = "🔴" if "-" in ml.split(":")[-1] else "🟢"
            block.append(f"{cue} {ml}")
        total = _sum_mtm(mtm_lines)
        tcue = "🟢" if total >= 0 else "🔴"
        out.append("```\n" + "\n".join(block) + "\n```")
        out.append(f"{tcue} **Total unrealized:** {total:+.2f}")
    else:
        out.append("_no open Polymarket positions_")

    # Strategy validation — status emojis + fenced block for column alignment
    out.append("\n📈 **Strategy validation**")
    report = (promotion_report or "").strip()
    if report:
        report = (report.replace("ready", "✅ ready")
                        .replace("failing", "❌ failing")
                        .replace("accumulating", "⏳ accumulating"))
        out.append("```\n" + report + "\n```")
    else:
        out.append("_no resolved positions yet_")

    # Data sources — single ✅/❌ line
    if source_health:
        health = " · ".join(f"{n} {'✅' if up else '❌'}" for n, up in source_health.items())
        out.append(f"\n🩺 **Data sources:** {health}")

    return "\n".join(out)


def _sum_mtm(mtm_lines: List[str]) -> float:
    """Extract and sum dollar amounts from formatted mtm lines."""
    total = 0.0
    for line in mtm_lines:
        # Lines look like: "pm:123 NO Artist: +$6.48"
        # Find the last token that matches a dollar amount
        parts = line.split()
        for part in reversed(parts):
            part = part.lstrip("+")
            if part.startswith("$") or part.startswith("-$"):
                try:
                    total += float(part.replace("$", ""))
                    break
                except ValueError:
                    pass
    return round(total, 2)


# ---------------------------------------------------------------------------
# Async I/O helpers
# ---------------------------------------------------------------------------

async def pm_current_yes_price(
    http: Any,
    pm_market_id: str,
    gamma_url: str = _GAMMA_DEFAULT,
) -> Optional[float]:
    """Fetch the current YES price for a Polymarket market via Gamma API.

    Strips the "pm:" prefix if present. Returns None on any error.
    """
    market_id = pm_market_id.removeprefix("pm:")
    url = f"{gamma_url}/markets/{market_id}"
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        data = resp.json()
        prices = _parse_json_list(data.get("outcomePrices"))
        if not prices:
            return None
        return float(prices[0])
    except Exception as exc:
        logger.debug("pm_current_yes_price error for %s: %s", pm_market_id, exc)
        return None


async def _check_source(http: Any, url: str) -> bool:
    """Return True if a HEAD/GET to url succeeds within a short timeout."""
    try:
        resp = await http.get(url, timeout=_HEALTH_TIMEOUT)
        return True
    except Exception:
        return False


async def _source_health(http: Any, gamma_url: str = _GAMMA_DEFAULT) -> Dict[str, bool]:
    """Ping kworb, gamma, and (optionally) spotify token endpoint."""
    health: Dict[str, bool] = {}
    health["kworb"] = await _check_source(http, _KWORB_URL)
    health["gamma"] = await _check_source(http, f"{gamma_url}/markets?limit=1")
    if os.environ.get("SPOTIFY_CLIENT_ID"):
        health["spotify"] = await _check_source(http, _SPOTIFY_TOKEN_URL)
    return health


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def gather_and_send(
    store: Any,
    http: Any,
    *,
    alert: Optional[Callable] = None,
) -> str:
    """Gather all digest sections, compose the text, send via alert, and return it."""
    from core import alerts as _alerts

    if alert is None:
        alert = _alerts.notify

    open_positions: List[Any] = []
    try:
        open_positions = store.open_positions()
    except Exception as exc:
        logger.warning("digest: could not fetch open positions: %s", exc)

    by_strategy: Dict[str, int] = {}
    for pos in open_positions:
        strat = getattr(pos, "strategy", "unknown")
        by_strategy[strat] = by_strategy.get(strat, 0) + 1

    mtm_lines: List[str] = []
    for pos in open_positions:
        mid = getattr(pos, "market_id", "")
        if not mid.startswith("pm:"):
            continue
        try:
            yes_price = await pm_current_yes_price(http, mid)
            if yes_price is None:
                mtm_lines.append(f"{mid} {pos.side}: (price unavailable)")
                continue
            pnl = position_mtm(pos.entry_price, pos.side, pos.size, yes_price)
            mtm_lines.append(f"{mid} {pos.side}: {pnl:+.2f}")
        except Exception as exc:
            logger.warning("digest: mtm error for %s: %s", mid, exc)
            mtm_lines.append(f"{mid} {pos.side}: (error)")

    promotion_report = "(no data)"
    try:
        from core.directional.validation import build_report
        promotion_report = build_report(store)
    except Exception as exc:
        logger.warning("digest: promotion report error: %s", exc)

    source_health: Dict[str, bool] = {}
    try:
        source_health = await _source_health(http)
    except Exception as exc:
        logger.warning("digest: source health error: %s", exc)

    text = digest_text(
        open_count=len(open_positions),
        by_strategy=by_strategy,
        mtm_lines=mtm_lines,
        promotion_report=promotion_report,
        source_health=source_health,
    )

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        await alert(
            "daily_digest",
            "Daily paper digest",
            text,
            severity="info",
            dedup_key=f"digest:{today}",
        )
    except Exception as exc:
        logger.warning("digest: alert send failed: %s", exc)

    return text
