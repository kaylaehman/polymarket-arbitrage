"""Structural-bias parameters from Jon-Becker/prediction-market-analysis (MIT).
Markets are well-calibrated overall, so these are SECONDARY tiebreakers, not a
primary signal. Magnitudes are conservative approximations of the paper's figures
and should be re-validated against fresh data before sizing up."""

# NO-minus-YES EV advantage (cents) by YES-price bucket; positive => NO favored.
_YES_NO_EV_BIAS = {5: 8.0, 10: 5.0, 20: 3.0, 50: 0.0, 80: -2.0, 90: -3.0}

# Maker/NO excess edge by category (fraction); Sports largest, Finance smallest.
_CATEGORY_MAKER_EDGE = {
    "Sports": 0.04,
    "Politics": 0.02,
    "Entertainment": 0.02,
    "Crypto": 0.01,
    "Finance": 0.005,
}


def _interp_bias(yes_price_cents: float) -> float:
    pts = sorted(_YES_NO_EV_BIAS.items())
    if yes_price_cents <= pts[0][0]:
        return pts[0][1]
    if yes_price_cents >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= yes_price_cents <= x1:
            t = (yes_price_cents - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return 0.0


def structural_score(price: float, side: str, category: str) -> float:
    yes_cents = price * 100 if side == "YES" else (1 - price) * 100
    bias_cents = _interp_bias(yes_cents)          # >0 => NO favored at this YES price
    directional = (bias_cents if side == "NO" else -bias_cents) / 100.0
    cat_edge = _CATEGORY_MAKER_EDGE.get(category, 0.005)
    # category edge is NO-side-only by design: maker/NO excess edge from repo#1 findings
    return directional + (cat_edge if side == "NO" else 0.0)
