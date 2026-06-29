"""
Kelly Criterion Position Sizing
===============================

Mathematically optimal position sizing based on edge and estimated win
probability (FEAT-05). Uses a fractional Kelly (quarter-Kelly by default) to
reduce variance, with a hard cap on the fraction of bankroll per market.

WARNING: Kelly sizing amplifies the cost of a miscalibrated probability.
Do not enable in production until the signal database (FEAT-09) shows the AI's
probability estimates are actually calibrated. The bot ships with
``trading.kelly_enabled: false``.
"""


def kelly_fraction(
    edge: float,
    yes_price: float,
    ai_probability: float,
    confidence: float,
    fraction: float = 0.25,
    max_fraction: float = 0.10,
) -> float:
    """Fraction of available capital to deploy on an opportunity.

    Args:
        edge: net edge after fees (e.g. 0.07 for 7%).
        yes_price: current market price for YES (0-1).
        ai_probability: Claude's estimated true probability for YES.
        confidence: Claude's confidence in that estimate (0-1).
        fraction: fractional-Kelly multiplier (0.25 = quarter-Kelly).
        max_fraction: hard ceiling on bankroll fraction per market.

    Returns a value in ``[0.0, max_fraction]``. Returns 0.0 for degenerate
    inputs (non-positive odds or probability).

    The win probability ``p`` uses the AI estimate when confidence is high
    enough (>= 0.6); otherwise it falls back to the market-implied probability
    nudged by the edge, so a low-confidence signal can't distort sizing.
    """
    if (confidence or 0.0) >= 0.6:
        p = ai_probability
    else:
        p = yes_price + edge

    # Guard against out-of-range prices/probabilities.
    if yes_price <= 0 or yes_price >= 1:
        return 0.0
    if p <= 0 or p >= 1:
        return 0.0

    q = 1 - p
    b = (1 / yes_price) - 1  # net decimal odds implied by the price

    if b <= 0:
        return 0.0

    raw_kelly = (b * p - q) / b
    fractional = raw_kelly * fraction
    return max(0.0, min(fractional, max_fraction))
