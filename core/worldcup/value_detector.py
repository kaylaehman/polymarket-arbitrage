"""
core/worldcup/value_detector.py — Compare model probabilities to PM.US prices.

Identifies markets where our simulated probability exceeds the market-implied
probability by at least VALUE_MARGIN.

EXPERIMENTAL / PAPER only.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.worldcup.config import VALUE_MARGIN, KELLY_FRACTION, PAPER_BANKROLL


@dataclass
class ValueBet:
    """A market where model prob exceeds market-implied prob by VALUE_MARGIN."""
    slug: str
    outcome_type: str
    team_slug: Optional[str]
    model_prob: float        # our simulated probability
    market_price: float      # PM.US yes_ask (0–1)
    edge: float              # model_prob - market_price
    kelly_stake: float       # fractional Kelly * PAPER_BANKROLL
    raw_market: dict         # original market dict from PM.US API


def detect_value(
    sim_probs: dict[str, float],
    markets: list[dict],
    value_margin: float = VALUE_MARGIN,
    kelly_fraction: float = KELLY_FRACTION,
    paper_bankroll: float = PAPER_BANKROLL,
) -> list[ValueBet]:
    """
    Match simulation probabilities against live PM.US market prices.

    Parameters
    ----------
    sim_probs     : {team_slug: win_probability} from simulate.simulate_tournament()
                    or simulate.simulate_stage_probabilities()
    markets       : raw market dicts from PM.US /v1/markets (already filtered to WC)
    value_margin  : minimum edge required (default VALUE_MARGIN from config)
    kelly_fraction: fractional Kelly to apply (default KELLY_FRACTION)
    paper_bankroll: paper account size for stake calculation

    Returns
    -------
    List of ValueBet, sorted by edge descending.
    """
    import importlib.util as _ilu
    import sys as _sys
    from pathlib import Path as _Path

    # scripts/ may not be a package (no __init__.py); load via file path.
    if "scripts.pmus_wc" in _sys.modules:
        _mod = _sys.modules["scripts.pmus_wc"]
    else:
        _scripts_dir = _Path(__file__).resolve().parents[2] / "scripts"
        _spec = _ilu.spec_from_file_location("scripts.pmus_wc", _scripts_dir / "pmus_wc.py")
        _mod = _ilu.module_from_spec(_spec)
        _sys.modules["scripts.pmus_wc"] = _mod
        _spec.loader.exec_module(_mod)

    parse_slug = _mod.parse_slug
    is_model_priceable = _mod.is_model_priceable

    value_bets: list[ValueBet] = []

    for market in markets:
        slug = market.get("slug", "")
        if not slug:
            continue

        key = parse_slug(slug)
        if not is_model_priceable(key):
            continue

        team_slug = key.team_slug
        if team_slug is None or team_slug not in sim_probs:
            continue

        model_p = sim_probs[team_slug]

        # Extract yes-side ask price from marketSides
        price = _extract_yes_price(market)
        if price is None or price <= 0.0 or price >= 1.0:
            continue

        edge = model_p - price
        if edge < value_margin:
            continue

        # Fractional Kelly: f = (edge / price) * kelly_fraction
        # Capped at kelly_fraction itself (never over-bet)
        kelly_full = edge / price
        stake_fraction = min(kelly_full * kelly_fraction, kelly_fraction)
        stake = stake_fraction * paper_bankroll

        value_bets.append(ValueBet(
            slug=slug,
            outcome_type=key.outcome_type,
            team_slug=team_slug,
            model_prob=round(model_p, 4),
            market_price=round(price, 4),
            edge=round(edge, 4),
            kelly_stake=round(stake, 2),
            raw_market=market,
        ))

    value_bets.sort(key=lambda b: b.edge, reverse=True)
    return value_bets


def _extract_yes_price(market: dict) -> Optional[float]:
    """
    Pull the YES (long=true) price from a PM.US market dict.

    PM.US marketSides uses:
      - description="Yes" + long=True  for the YES side
      - price field is the current mid-price (no separate ask/bid in this API)

    Falls back to outcomePrices[0] if marketSides unavailable.
    """
    # Try marketSides first — PM.US format: long=True is YES side
    sides = market.get("marketSides") or market.get("market_sides") or []
    for side in sides:
        if not isinstance(side, dict):
            continue
        # PM.US uses long=True for YES side; also accept description="Yes"
        is_yes = side.get("long") is True or (
            str(side.get("description", "")).upper() in ("YES", "1")
        )
        # Also accept explicit outcome field (used in some responses)
        if not is_yes:
            outcome = str(side.get("outcome", "")).upper()
            is_yes = outcome in ("YES", "1")
        if is_yes:
            price = side.get("ask") or side.get("price")
            if price is not None:
                try:
                    return float(price)
                except (ValueError, TypeError):
                    pass

    # Fallback: outcomePrices[0]
    prices = market.get("outcomePrices") or market.get("outcome_prices") or []
    if prices:
        try:
            return float(prices[0])
        except (ValueError, TypeError):
            pass

    return None
