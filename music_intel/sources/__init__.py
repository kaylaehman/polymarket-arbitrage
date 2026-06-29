"""
music_intel.sources — Chart data source adapters.

Exports the public interface (ChartDataSource, ChartRecord) so callers can
import from this package without knowing the internal layout.
"""

from music_intel.sources.base import ChartDataSource, ChartRecord

__all__ = ["ChartDataSource", "ChartRecord"]
