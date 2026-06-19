"""Strategy ABC for directional trading.

All strategies implement the same interface so the DirectionalEngine can
dispatch to them uniformly without knowing their internal logic.
"""
from __future__ import annotations

import abc
from typing import Any

from core.directional.models import DirectionalCandidate


class Strategy(abc.ABC):
    """Abstract base for a directional trading strategy.

    Subclasses must implement:
    - ``name`` — unique identifier string used in logging and candidate tagging.
    - ``scan(markets, ctx)`` — async method that returns DirectionalCandidate
      objects for markets that pass the strategy's signal threshold.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique strategy identifier (e.g. "safe_compounder")."""

    @abc.abstractmethod
    async def scan(
        self,
        markets: list,
        ctx: dict[str, Any],
    ) -> list[DirectionalCandidate]:
        """Evaluate a list of KalshiMarket objects and return candidates.

        Args:
            markets: Pre-filtered list of KalshiMarket objects from the scanner.
            ctx: Shared context dict.  At minimum contains:
                ``no_ask``: ``Callable[[str], float | None]`` — returns the
                current NO ask price for a given ticker (or None if unavailable).

        Returns:
            List of DirectionalCandidate objects whose edge meets the strategy's
            threshold.  Empty list when no opportunities are found.
        """
