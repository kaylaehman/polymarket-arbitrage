"""
core/worldcup/config.py — Configuration constants for World Cup value-betting experiment.

EXPERIMENTAL / PAPER only. Not connected to live trading.
"""
from __future__ import annotations

VALUE_MARGIN = 0.05  # observation threshold (loosened from 0.07; paper-only, expect noise)
KELLY_FRACTION = 0.20
PAPER_BANKROLL = 500.0
MIN_LIQUIDITY = 50
MARKET_TYPES = ["tournament_winner", "group_winner", "stage_of_elimination"]
N_SIMULATIONS = 20000
DB_PATH = "data/worldcup_paper.db"
WC_MODEL_PATH_DEFAULT = "/home/kayla/wc-model"
