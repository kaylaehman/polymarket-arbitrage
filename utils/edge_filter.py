"""Confidence-tiered edge filter (ported from kalshi-ai-bot, MIT)."""


def passes_edge(confidence: float, edge: float) -> bool:
    """Return True iff the edge is sufficient for the given confidence tier.

    Tiers:
        conf >= 0.80 -> edge >= 0.03
        conf >= 0.60 -> edge >= 0.05
        conf >= 0.35 -> edge >= 0.08
        conf < 0.35  -> always False
    """
    if confidence < 0.35:
        return False
    if confidence >= 0.80:
        return edge >= 0.03
    if confidence >= 0.60:
        return edge >= 0.05
    return edge >= 0.08
