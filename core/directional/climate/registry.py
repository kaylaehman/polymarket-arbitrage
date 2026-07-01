"""Registry that dispatches a market to the first matching ClimateProvider."""
from __future__ import annotations
from typing import Any, List, Optional, Tuple
from core.directional.climate.base import ClimateProvider, ParsedClimate


class ClimateRegistry:
    def __init__(self, providers: List[ClimateProvider]):
        self._providers = providers

    def match(self, market: Any) -> Optional[Tuple[ClimateProvider, ParsedClimate]]:
        for p in self._providers:
            try:
                parsed = p.match(market)
            except Exception:
                parsed = None
            if parsed is not None:
                return (p, parsed)
        return None
