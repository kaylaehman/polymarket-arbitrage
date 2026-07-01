"""
Alerter — fire-and-forget push notifications to Discord and/or Telegram.

Module-level singleton pattern:
    from core import alerts
    alerts.configure(alerter_instance)
    asyncio.create_task(alerts.notify("kill_switch", "Kill switch", reason, "critical"))

Each channel send is wrapped in try/except + short httpx timeout — never raises.
Per-(event_type, dedup_key) cooldown suppresses repeat noise.
Critical-severity alerts always send (bypass cooldown) and always pass the
min_severity gate.
"""

from __future__ import annotations

import logging
import asyncio
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

    # Severity rank order (ascending); critical is always treated specially.
    _SEVERITY_RANK: dict[str, int] = {
        "debug": 0,
        "info": 1,
        "warning": 2,
        "warn": 2,
        "critical": 3,
    }

    def __init__(
        self,
        discord_webhook: Optional[str],
        telegram_bot_token: Optional[str],
        telegram_chat_id: Optional[str],
        cooldown_seconds: float = 60.0,
        min_severity: str = "info",
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._discord = discord_webhook or None
        self._tg_token = telegram_bot_token or None
        self._tg_chat = telegram_chat_id or None
        self._cooldown = cooldown_seconds
        self._min_severity = min_severity
        self._now = now_fn
        # dedup store: (event_type, dedup_key) -> last send monotonic ts
        self._last_sent: dict[tuple[str, str], float] = {}
        # Discord webhook rate-limit protection: serialize sends and space them
        # so a burst (batch settlement, a multi-leg place cycle) doesn't trip
        # Discord's per-webhook 429 and silently drop alerts.
        self._discord_lock = asyncio.Lock()
        self._discord_min_interval = 1.1   # seconds between Discord sends
        self._discord_last_send = 0.0      # monotonic ts of last Discord POST

    async def send(
        self,
        event_type: str,
        title: str,
        body: str,
        severity: str = "info",
        dedup_key: str = "",
    ) -> list[str]:
        """Send alert to each configured channel, respecting cooldown.

        Critical alerts always send regardless of cooldown or min_severity.
        Non-critical alerts are dropped when below min_severity or within cooldown.

        Returns:
            List of channel names that were attempted (may be empty if
            within cooldown / below threshold / no channels configured).
        """
        if not self._discord and not (self._tg_token and self._tg_chat):
            return []

        is_critical = severity == "critical"

        # Severity gate: drop messages below min_severity (critical always passes).
        if not is_critical:
            sev_rank = self._SEVERITY_RANK.get(severity, 1)
            min_rank = self._SEVERITY_RANK.get(self._min_severity, 1)
            if sev_rank < min_rank:
                return []

        # Per-(event_type, dedup_key) cooldown; critical bypasses but still records.
        cache_key = (event_type, dedup_key)
        now = self._now()

        if not is_critical:
            last = self._last_sent.get(cache_key)
            if last is not None and (now - last) < self._cooldown:
                return []

        # I3: opportunistically evict stale entries to bound dict size.
        if len(self._last_sent) > 512:
            evict_age = max(self._cooldown, 3600.0)
            stale = [k for k, ts in self._last_sent.items() if (now - ts) >= evict_age]
            for k in stale:
                del self._last_sent[k]

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
        """POST to Discord webhook, rate-limit safe; swallows all exceptions.

        Sends are serialized (lock) and spaced by ``_discord_min_interval`` so a
        burst can't trip Discord's per-webhook 429. On a 429 we honour the
        ``Retry-After`` header and retry once, so an alert that hits the limit is
        delayed rather than dropped.
        """
        try:
            async with self._discord_lock:
                # Space sends to respect the webhook rate limit.
                gap = time.monotonic() - self._discord_last_send
                if gap < self._discord_min_interval:
                    await asyncio.sleep(self._discord_min_interval - gap)
                async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
                    resp = await client.post(self._discord, json={"content": text})
                    if resp.status_code == 429:
                        retry_after = 1.0
                        try:
                            retry_after = float(resp.headers.get("Retry-After", "1"))
                        except (TypeError, ValueError):
                            pass
                        await asyncio.sleep(min(retry_after, 10.0) + 0.1)
                        resp = await client.post(self._discord, json={"content": text})
                    resp.raise_for_status()
                self._discord_last_send = time.monotonic()
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
