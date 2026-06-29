"""Billboard published-chart source — GROUND TRUTH / backtest only.

Wired ONLY into music_intel/calibration.py; NEVER imported by
music_intel/projection.py, so actual Billboard results cannot leak into the live
projection inputs. Uses the billboard.py library (guoguo12), which scrapes
billboard.com.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from music_intel.sources.base import ChartDataSource, ChartRecord

logger = logging.getLogger(__name__)

_CHART_NAMES = {"hot100": "hot-100", "billboard200": "billboard-200"}


class BillboardSource(ChartDataSource):
    """Actual published Hot 100 / Billboard 200 results (backtest ground truth)."""

    @property
    def name(self) -> str:
        return "billboard"

    @property
    def trust_tier(self) -> int:
        return 2

    def _fetch_chart(self, chart_name: str, date_str: Optional[str]):
        """Isolated blocking call to the billboard library (patched in tests)."""
        import billboard
        return billboard.ChartData(chart_name, date=date_str)

    async def fetch(
        self, chart: str, as_of: Optional[datetime.date] = None
    ) -> list[ChartRecord]:
        chart_name = _CHART_NAMES.get(chart)
        if chart_name is None:
            logger.debug("[billboard] unknown chart %s", chart)
            return []
        date_str = as_of.isoformat() if as_of else None
        try:
            data = self._fetch_chart(chart_name, date_str)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[billboard] fetch %s error: %s", chart, exc)
            return []

        as_of_date = as_of or datetime.date.today()
        records: list[ChartRecord] = []
        for entry in getattr(data, "entries", []):
            try:
                last = getattr(entry, "lastPos", 0) or 0
                rank = int(entry.rank)
                delta = (rank - last) if last else None  # negative = moved up (base.py convention)
                records.append(ChartRecord(
                    source="billboard", chart=chart, as_of=as_of_date, rank=rank,
                    title=entry.title, artist=entry.artist,
                    peak=getattr(entry, "peakPos", None),
                    days_on_chart=getattr(entry, "weeks", None),
                    rank_delta=delta,
                ))
            except Exception:  # noqa: BLE001
                continue
        return records
