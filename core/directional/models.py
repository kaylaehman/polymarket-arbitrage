"""Directional trading dataclasses.

These are the core data types used throughout the directional trading module.
All three expose the fields required by the risk Protocol:
  DirectionalOrder: .market_id, .notional, .side
  DirectionalPosition: .notional (populated from DirectionalOrder.notional at open time)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DirectionalCandidate:
    """A market candidate identified by the scanner as a potential directional trade."""
    market_id: str
    title: str
    category: str
    side: str
    market_price: float
    ai_probability: float | None
    confidence: float | None
    edge: float
    strategy: str
    reasoning: str = ""


@dataclass
class DirectionalOrder:
    """An order ready to be placed (paper or live)."""
    market_id: str
    side: str
    price: float
    size: int
    notional: float
    strategy: str
    reasoning: str = ""


@dataclass
class DirectionalPosition:
    """An open or closed directional position."""
    market_id: str
    side: str
    entry_price: float
    size: int
    strategy: str
    mode: str
    opened_at: datetime
    stop_loss: float | None
    take_profit: float | None
    notional: float = 0.0
    status: str = "open"
    order_id: str | None = None  # live maker: resting order_id until filled/cancelled
