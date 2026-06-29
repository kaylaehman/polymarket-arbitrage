"""Pluggable alert sink for music_intel. Default sink forwards to core.alerts
(Telegram/Discord); a CollectingSink is provided for tests. Sinks must never
raise into the engine.
"""
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class AlertSink(Protocol):
    async def emit(self, *, title: str, body: str, dedup_key: str) -> None: ...


class CoreAlertSink:
    """Forwards to the repo's core.alerts.notify (Telegram-capable)."""

    async def emit(self, *, title: str, body: str, dedup_key: str) -> None:
        try:
            from core.alerts import notify
            await notify("chart_edge", title, body, severity="info", dedup_key=dedup_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[music-intel] alert emit failed: %s", exc)


class CollectingSink:
    """Test sink — records emitted alerts instead of sending them."""

    def __init__(self) -> None:
        self.alerts: list[dict] = []

    async def emit(self, *, title: str, body: str, dedup_key: str) -> None:
        self.alerts.append({"title": title, "body": body, "dedup_key": dedup_key})
