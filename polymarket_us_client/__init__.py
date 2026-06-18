"""
Polymarket.US API Client Module
================================

REST-based client for the Polymarket.US trading platform.
Drop-in replacement implementing BasePolymarketClient.
Disabled by default (mode.polymarket_us_enabled: false).
"""

from polymarket_us_client.api import PolymarketUSClient

__all__ = ["PolymarketUSClient"]
