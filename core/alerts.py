"""
Alerter — fire-and-forget push notifications to Discord and/or Telegram.

Module-level singleton pattern:
    from core import alerts
    alerts.configure(alerter_instance)
    asyncio.create_task(alerts.notify("kill_switch", "Kill switch", reason, "critical"))

Each channel send is wrapped in try/except + short httpx timeout — never raises.
Per-(event_type, dedup_key) cooldown suppresses repeat noise.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

logger = logging.getLogger(__name__)

# Module-level singleton; None = not configured / no-op
_ALERTER: Optional["Alerter"] = None


def configure(alerter: "Alerter") -> None:
    """Set the module-level singleton Alerter."""
    global _ALERTER
    _ALERTER = alerter


async def notify(
    event_type: str,
    title: str,
    body: str,
    severity: str = "info",
    dedup_key: str = "",
) -> None:
    """Fire-and-forget notification via the configured Alerter.

    No-op (and never raises) when no Alerter has been configured.
    Callers should wrap in asyncio.create_task() so they don't block.
    """
    if _ALERTER is None:
        return
    try:
        await _ALERTER.send(event_type, title, body, severity=severity, dedup_key=dedup_key)
    except Exception:  # pragma: no cover — belt-and-suspenders
        pass


class Alerter:
    """Send formatted alerts to Discord webhook and/or Telegram bot.

    Args:
        discord_webhook: Full Discord webhook URL, or None to skip.
        telegram_bot_token: Telegram bot token (the "123456:ABC..." part), or None.
        telegram_chat_id: Telegram chat/channel ID, or None.
        cooldown_seconds: Min seconds between sends for the same (event_type, dedup_key).
        now_fn: Injectable monotonic clock for testing.
    """

    _TIMEOUT = 5.0  # httpx timeout for each channel send

    def __init__(
        self,
        discord_webhook: Optional[str],
        telegram_bot_token: Optional[str],
        telegram_chat_id: Optional[str],
        cooldown_seconds: float = 60.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._discord = discord_webhook or None
        self._tg_token = telegram_bot_token or None
        self._tg_chat = telegram_chat_id or None
        self._cooldown = cooldown_seconds
        self._now = now_fn
        # dedup store: (event_type, dedup_key) -> last send monotonic ts
        self._last_sent: dict[tuple[str, str], float] = {}

    async def send(
        self,
        event_type: str,
        title: str,
        body: str,
        severity: str = "info",
        dedup_key: str = "",
    ) -> list[str]:
        """Send alert to each configured channel, respecting cooldown.

        Returns:
            List of channel names that were attempted (may be empty if
            within cooldown or no channels configured).
        """
        if not self._discord and not (self._tg_token and self._tg_chat):
            return []

        # Per-(event_type, dedup_key) cooldown
        cache_key = (event_type, dedup_key)
        now = self._now()
        last = self._last_sent.get(cache_key)
        if last is not None and (now - last) < self._cooldown:
            return []

        self._last_sent[cache_key] = now

        attempted: list[str] = []
        text = f"[{severity}] {title}\n{body}"

        if self._discord:
            attempted.append("discord")
            await self._send_discord(text)

        if self._tg_token and self._tg_chat:
            attempted.append("telegram")
            await self._send_telegram(text)

        return attempted

    async def _send_discord(self, text: str) -> None:
        """POST to Discord webhook; swallows all exceptions."""
        try:
            async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
                resp = await client.post(self._discord, json={"content": text})
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("[alerts] Discord send failed: %s", exc)

    async def _send_telegram(self, text: str) -> None:
        """POST to Telegram sendMessage; swallows all exceptions."""
        url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
                resp = await client.post(
                    url,
                    json={"chat_id": self._tg_chat, "text": text},
                )
                resp.raise_for_status()
        except Exception as exc:
            logger.warning("[alerts] Telegram send failed: %s", exc)
