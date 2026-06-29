"""Luminate adapter — env-gated STUB (high-confidence source seam).

Luminate (api.luminatedata.com) is the paid, authoritative source. We have no
access yet, so this ships DISABLED unless LUMINATE_API_KEY is provided, and even
then returns [] from a clean stub. The seam exists so it can become the
trust_tier=3 source later and downrank the scraped feeds.
"""
from __future__ import annotations

import datetime
import logging
from typing import Any, Optional

from music_intel.sources.base import ChartDataSource, ChartRecord

logger = logging.getLogger(__name__)

_LUMINATE_BASE = "https://api.luminatedata.com"


class LuminateSource(ChartDataSource):
    def __init__(self, http: Any = None, api_key: Optional[str] = None) -> None:
        self._http = http
        self._key = api_key

    @property
    def name(self) -> str:
        return "luminate"

    @property
    def trust_tier(self) -> int:
        return 3

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def fetch(
        self, chart: str, as_of: Optional[datetime.date] = None
    ) -> list[ChartRecord]:
        if not self.enabled:
            return []
        # Seam only: when a key is present we WOULD GET {_LUMINATE_BASE}/charts/...
        # with headers {"x-api-key": self._key}. Not implemented (no access yet).
        logger.info("[luminate] enabled but endpoint not implemented (stub) — chart=%s", chart)
        return []
