#!/usr/bin/env python3
"""Print the maker fill-rate report from the directional store."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.directional.store import DirectionalStore

db = sys.argv[1] if len(sys.argv) > 1 else "data/directional.db"
store = DirectionalStore(db)

# Fetch stats for the default strategy
stats = store.maker_fill_stats(strategy="maker_longshot")

# Format and print report
print("=" * 60)
print("MAKER FILL-RATE REPORT (maker_longshot)")
print("=" * 60)
print()
print(f"Pending (resting, unfilled):     {stats['pending']}")
print(f"Filled (open, held):             {stats['filled_open']}")
print(f"Filled & Settled (closed):       {stats['filled_settled']}")
print(f"Unfilled (never entered):        {stats['unfilled']}")
print()
print(f"Fill Rate:     {stats['fill_rate']:.4f}" if stats['fill_rate'] is not None else "Fill Rate:     N/A (no unfilled or filled)")
print(f"Win Rate:      {stats['win_rate_filled']:.4f}" if stats['win_rate_filled'] is not None else "Win Rate:      N/A (no settled positions)")
print()
print("=" * 60)
