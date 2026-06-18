"""
Outcome Poller
==============

Periodically checks whether markets we logged signals for have resolved, and
records the YES/NO outcome in the signal database (completes FEAT-09).

Without this, ``SignalDB.get_signal_accuracy`` has nothing to compare signals
against. The poll is best-effort: a market that can't be fetched or whose
resolution can't be determined is simply skipped and retried next pass.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 3600.0  # hourly, per spec


class OutcomePoller:
    """Polls market resolution status and logs outcomes to the SignalDB."""

    def __init__(self, client, db, interval_seconds: float = _DEFAULT_INTERVAL):
        """
        Args:
            client: a Polymarket client exposing ``async get_market(market_id)``.
            db: a SignalDB instance.
            interval_seconds: how often to poll (default hourly).
        """
        self.client = client
        self.db = db
        self.interval_seconds = interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def poll_once(self) -> int:
        """Run one resolution pass. Returns the number of outcomes newly logged."""
        logged = 0
        for market_id in self.db.get_unresolved_market_ids():
            try:
                market = await self.client.get_market(market_id)
            except Exception as e:  # noqa: BLE001 — skip this market, retry next pass
                logger.debug("[OutcomePoller] fetch failed for %s: %s", market_id, e)
                continue

            if market and getattr(market, "resolved", False) and market.resolution in ("YES", "NO"):
                self.db.log_outcome(
                    market_id,
                    resolved_yes=(market.resolution == "YES"),
                    resolution_date=getattr(market, "end_date", None),
                )
                logged += 1

        if logged:
            logger.info("[OutcomePoller] Logged %d newly resolved market(s)", logged)
        return logged

    async def start(self) -> None:
        """Start the background polling loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="outcome_poller")
        logger.info("[OutcomePoller] Started (every %.0fs)", self.interval_seconds)

    async def _run(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never let the loop die
                logger.warning("[OutcomePoller] poll error: %s", e)

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
