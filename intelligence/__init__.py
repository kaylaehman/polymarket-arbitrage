"""
Intelligence Layer
==================

AI news-reading layer for the Polymarket arbitrage bot.

This package ingests recent news headlines for each market topic, asks Claude
whether current odds reflect reality, and produces a `SignalSummary` the arb
engine can use to filter or boost opportunities.

Design rules (see intelligence/INTELLIGENCE.md and CORE_HOOKS.md):
- This package NEVER imports from ``core/`` — only ``core/`` imports from here.
- Every public call is async and advisory: callers must tolerate failure.
- ``IntelligenceEngine`` is the only class ``core/`` should import.
"""

from intelligence.signal import MarketSignal, SignalSummary, classify_direction

__all__ = ["MarketSignal", "SignalSummary", "classify_direction"]
