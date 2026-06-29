#!/usr/bin/env python
"""Daily paper-trading digest → Discord. Cron entry.

Runs on the HOST (reads the bind-mounted data/directional.db + .env for the
Discord webhook), builds the digest, and sends it via the configured Alerter.
Read-only: never trades, never edits the store. Usage (cron loads .env first):

  cd /home/kayla/docker/polymarket-arbitrage \\
    && set -a && . ./.env && set +a \\
    && .venv-dev/bin/python scripts/daily_digest.py >> logs/digest.log 2>&1
"""
import asyncio
import os
import sys

# Ensure the repo root is importable when run as a bare script (cron).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from core import alerts
from core.alerts import Alerter
from core.directional.digest import gather_and_send
from core.directional.store import DirectionalStore
from utils.config_loader import load_config

_UA = "music-intel/1.0 (+https://kaylas.systems; contact kaylaehman@pm.me)"


async def main() -> None:
    cfg = load_config()
    alerts_cfg = getattr(cfg, "alerts", None)

    # Configure the same Alerter the bot uses (webhook/tokens from env).
    alerts.configure(Alerter(
        discord_webhook=os.getenv("ALERT_DISCORD_WEBHOOK"),
        telegram_bot_token=os.getenv("ALERT_TELEGRAM_TOKEN"),
        telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID"),
        cooldown_seconds=getattr(alerts_cfg, "cooldown_seconds", 60.0) if alerts_cfg else 60.0,
        min_severity=getattr(alerts_cfg, "min_severity", "info") if alerts_cfg else "info",
    ))

    store = DirectionalStore(db_path=cfg.directional.db_path)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as http:
        text = await gather_and_send(store, http)

    # Also print so the cron log captures what was sent.
    print(text)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001 — cron must not crash loudly
        print(f"[daily_digest] failed: {exc}", file=sys.stderr)
        sys.exit(1)
