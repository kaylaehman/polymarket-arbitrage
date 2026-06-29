"""
music_intel.sources.base — Core interfaces for chart data sources.

Defines ChartRecord (immutable data container) and ChartDataSource (abstract
async fetch interface).  No network I/O lives here; concrete subclasses own
that.

Trust-tier convention (higher = more authoritative):
  luminate = 3  (paid API; ground truth when key present)
  billboard = 2 (published chart results; backtest/calibration only)
  kworb    = 1  (scraped; primary free source for live projections)
"""

import abc
import datetime
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class ChartRecord:
    """One row on a chart for one day — immutable value object.

    Required fields identify the chart position; optional fields carry
    streaming velocity data used by the projection engine.
    """

    source: str
    """Data source identifier, e.g. ``"kworb"``, ``"billboard"``."""

    chart: str
    """Chart slug, e.g. ``"hot100"``, ``"billboard200"``, ``"spotify_us_daily"``."""

    as_of: datetime.date
    """The date this snapshot represents (not the fetch date)."""

    rank: int
    """Chart rank (1 = #1)."""

    title: str
    """Track title."""

    artist: str
    """Primary artist(s) as a single string."""

    track_id: Optional[str] = None
    """Source-specific track identifier (Spotify URI, ISRC, etc.)."""

    rank_delta: Optional[int] = None
    """Position change since the previous snapshot (negative = moved up)."""

    streams_period: Optional[int] = None
    """Streams during the reporting period (day or week, source-dependent)."""

    streams_7day: Optional[int] = None
    """7-day rolling stream count when the source provides it."""

    days_on_chart: Optional[int] = None
    """Consecutive days or weeks the track has appeared on this chart."""

    peak: Optional[int] = None
    """All-time peak rank on this chart (1 = has reached #1)."""


class ChartDataSource(abc.ABC):
    """Abstract interface for chart data adapters.

    Each concrete subclass wraps one source (kworb, Billboard, Luminate …)
    and exposes a single async ``fetch`` method.  The ``name`` and
    ``trust_tier`` properties let the projection engine weight sources.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this source, e.g. ``"kworb"``."""

    @property
    @abc.abstractmethod
    def trust_tier(self) -> int:
        """Relative authority: luminate=3 > billboard=2 > kworb=1.

        Higher tier sources override lower ones when projecting.
        """

    @abc.abstractmethod
    async def fetch(
        self,
        chart: str,
        as_of: Optional[datetime.date] = None,
    ) -> list[ChartRecord]:
        """Fetch chart data for *chart* as of *as_of* (latest if None).

        Must never raise on a recoverable network/parsing error; callers rely
        on an empty list to indicate degraded-but-alive operation.

        Args:
            chart: Chart slug (``"hot100"``, ``"spotify_us_daily"``, …).
            as_of: Target date; ``None`` means the most recent available.

        Returns:
            Ordered list of ChartRecord (rank 1 first), possibly empty.
        """
