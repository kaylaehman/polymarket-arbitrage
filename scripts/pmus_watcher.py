"""
PM.US Watcher — ops script (NOT part of the trading bot).

Polls Polymarket.US for markets our edges can trade:
  (a) NEAR-TERM LONGSHOTS: active+open binary markets resolving within 30 days
      with at least one side priced in [0.05, 0.20] and two-sided orderbook.
  (b) ACTIVE CLIMATE/WEATHER markets: category=climate, active, not closed.

On first run: writes baseline state, sends a one-time baseline Discord note.
Subsequent runs: silent unless NEW markets appear (set diff vs saved state).
Never crashes; logs to stdout.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pmus_watcher] %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("pmus_watcher")

GATEWAY = "https://gateway.polymarket.us"
STATE_FILE = Path("/app/data/pmus_watcher_state.json")
NEAR_TERM_DAYS = 30
LONGSHOT_LOW = 0.05
LONGSHOT_HIGH = 0.20
PAGE_SIZE = 100
PAGE_DELAY = 0.3  # seconds between pages; PM.US rate-limits aggressively


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception as exc:
            log.warning(f"Could not read state file: {exc}; starting fresh")
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def _fetch_all_active_markets(client: httpx.AsyncClient) -> list[dict]:
    """Page through active+not-closed markets until exhausted."""
    markets: list[dict] = []
    offset = 0
    while True:
        try:
            resp = await client.get(
                f"{GATEWAY}/v1/markets",
                params={"limit": PAGE_SIZE, "offset": offset, "active": "true", "closed": "false"},
            )
            resp.raise_for_status()
        except Exception as exc:
            log.warning(f"Fetch stopped at offset={offset}: {exc}")
            break

        data = resp.json()
        batch = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        if not batch:
            break

        markets.extend(batch)
        log.debug(f"Fetched offset={offset} page={len(batch)} total={len(markets)}")
        offset += PAGE_SIZE
        await asyncio.sleep(PAGE_DELAY)

    log.info(f"Fetched {len(markets)} active+open markets from PM.US")
    return markets


def _classify_markets(markets: list[dict], now: datetime) -> tuple[list[dict], list[dict]]:
    """
    Returns (near_term_longshots, climate_markets).

    near_term_longshot: active, not closed, binary (2 sides), endDate within 30d,
                        at least one tradeable side price in [LONGSHOT_LOW, LONGSHOT_HIGH].
    climate: category=climate, active, not closed (already filtered by caller).
    """
    cutoff = now + timedelta(days=NEAR_TERM_DAYS)
    near_term: list[dict] = []
    climate: list[dict] = []

    for m in markets:
        cat = (m.get("category") or "").lower()
        slug = m.get("slug", m.get("id", ""))
        question = m.get("question", "")
        end_raw = m.get("endDate", "")

        if any(k in cat for k in ("climate", "weather")):
            climate.append({"slug": slug, "question": question, "end": end_raw, "category": cat})

        if not end_raw:
            continue
        try:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if end_dt > cutoff:
            continue

        sides = m.get("marketSides", [])
        tradeable_prices = [
            float(s["price"])
            for s in sides
            if s.get("tradable") and s.get("price") is not None
        ]
        if len(sides) != 2 or len(tradeable_prices) < 2:
            continue

        longshot_prices = [p for p in tradeable_prices if LONGSHOT_LOW <= p <= LONGSHOT_HIGH]
        if not longshot_prices:
            continue

        near_term.append({
            "slug": slug,
            "question": question,
            "end": end_raw,
            "prices": tradeable_prices,
            "category": cat,
        })

    return near_term, climate


def _build_discord_message(
    new_near_term: list[dict],
    new_climate: list[dict],
    total_near_term: int,
    total_climate: int,
    is_baseline: bool,
) -> str:
    if is_baseline:
        lines = [
            "**PM.US Watcher — baseline established**",
            f"Near-term longshots (<=30d, side in 5-20%): **{total_near_term}**",
            f"Active climate/weather markets: **{total_climate}**",
        ]
        if total_near_term > 0:
            lines.append("\nSample near-term longshots:")
            for m in new_near_term[:5]:
                lines.append(f"  • `{m['slug']}` — {m['question']} (ends {m['end'][:10]}, px {m['prices']})")
        if total_climate > 0:
            lines.append("\nSample climate markets:")
            for m in new_climate[:5]:
                lines.append(f"  • `{m['slug']}` — {m['question']}")
        if total_near_term > 0 or total_climate > 0:
            lines.append("\n**ACTION: flip on the PM.US scanner when ready.**")
        return "\n".join(lines)

    lines = ["**PM.US Watcher — NEW tradeable markets appeared**"]
    if new_near_term:
        lines.append(f"\nNEW near-term longshots: **{len(new_near_term)}** (total {total_near_term})")
        for m in new_near_term[:5]:
            lines.append(f"  • `{m['slug']}` — {m['question']} (ends {m['end'][:10]}, px {m['prices']})")
        if len(new_near_term) > 5:
            lines.append(f"  ... and {len(new_near_term) - 5} more")
    if new_climate:
        lines.append(f"\nNEW climate/weather markets: **{len(new_climate)}** (total {total_climate})")
        for m in new_climate[:5]:
            lines.append(f"  • `{m['slug']}` — {m['question']}")
        if len(new_climate) > 5:
            lines.append(f"  ... and {len(new_climate) - 5} more")
    lines.append("\n**ACTION: flip on the PM.US scanner.**")
    return "\n".join(lines)


async def _post_discord(webhook_url: str, message: str) -> None:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "polymarket-arb-watcher/1.0",
    }
    payload = {"content": message[:2000]}  # Discord 2000-char limit
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=payload, headers=headers)
            resp.raise_for_status()
            log.info("Discord alert sent")
    except Exception as exc:
        log.error(f"Discord post failed: {exc}")


async def main() -> None:
    webhook_url = os.getenv("ALERT_DISCORD_WEBHOOK", "")
    if not webhook_url:
        log.warning("ALERT_DISCORD_WEBHOOK not set; alerts will be skipped")

    now = datetime.now(timezone.utc)
    log.info(f"PM.US watcher run at {now.isoformat()}")

    async with httpx.AsyncClient(timeout=30) as client:
        markets = await _fetch_all_active_markets(client)

    near_term, climate = _classify_markets(markets, now)
    near_term_slugs = {m["slug"] for m in near_term}
    climate_slugs = {m["slug"] for m in climate}

    log.info(
        f"Classified: near_term_longshots={len(near_term)} climate={len(climate)}"
    )

    state = _load_state()
    is_first_run = "near_term_slugs" not in state

    prev_near_term = set(state.get("near_term_slugs", []))
    prev_climate = set(state.get("climate_slugs", []))

    new_near_term_slugs = near_term_slugs - prev_near_term
    new_climate_slugs = climate_slugs - prev_climate

    new_near_term = [m for m in near_term if m["slug"] in new_near_term_slugs]
    new_climate = [m for m in climate if m["slug"] in new_climate_slugs]

    # Update state
    new_state = {
        "last_run": now.isoformat(),
        "near_term_slugs": sorted(near_term_slugs),
        "climate_slugs": sorted(climate_slugs),
        "near_term_count": len(near_term),
        "climate_count": len(climate),
    }
    _save_state(new_state)
    log.info("State file updated")

    should_alert = (
        is_first_run
        or bool(new_near_term_slugs)
        or bool(new_climate_slugs)
    )

    if not should_alert:
        log.info(
            f"No new tradeable markets (near_term={len(near_term)}, "
            f"climate={len(climate)}); staying silent"
        )
        return

    msg = _build_discord_message(
        new_near_term=new_near_term if is_first_run else new_near_term,
        new_climate=new_climate if is_first_run else new_climate,
        total_near_term=len(near_term),
        total_climate=len(climate),
        is_baseline=is_first_run,
    )

    log.info(f"Sending alert: is_baseline={is_first_run} new_near_term={len(new_near_term_slugs)} new_climate={len(new_climate_slugs)}")
    log.info(f"Message preview: {msg[:200]}")

    if webhook_url:
        await _post_discord(webhook_url, msg)
    else:
        log.info("(No webhook configured, message not sent)")


if __name__ == "__main__":
    asyncio.run(main())
