"""music_intel configuration — calibrated-heuristic coefficients + gate knobs.

Self-contained so the live bot's config loader is never put at risk. Reads an
optional ``music_intel:`` block from the parsed config.yaml dict; every value has
a documented default. These coefficients/cutoffs are HEURISTICS — Billboard's
exact equivalent-unit math is not public.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from music_intel.edge import EdgeConfig


@dataclass
class MusicIntelConfig:
    enabled: bool = False
    charts: list = field(default_factory=lambda: ["hot100", "billboard200"])
    # projection coefficients (heuristics; see projection.py)
    stream_eu: float = 1250.0       # paid streams per chart unit
    sale_eu: float = 1.0
    airplay_per_unit: float = 0.0
    margin_k: float = 12.0
    # politeness
    request_min_interval_s: float = 2.0
    daily_call_cap: int = 500
    user_agent: str = "music-intel/1.0 (+https://kaylas.systems; contact kaylaehman@pm.me)"
    # edge gate
    edge: EdgeConfig = field(default_factory=EdgeConfig)
    # alerts
    alerts_enabled: bool = True

    @classmethod
    def from_dict(cls, root: dict | None) -> "MusicIntelConfig":
        block = (root or {}).get("music_intel", {}) or {}
        edge_block = block.get("edge", {}) or {}
        edge = EdgeConfig(**{k: v for k, v in edge_block.items()
                             if k in EdgeConfig.__dataclass_fields__})
        kwargs = {k: v for k, v in block.items()
                  if k in cls.__dataclass_fields__ and k != "edge"}
        return cls(edge=edge, **kwargs)
