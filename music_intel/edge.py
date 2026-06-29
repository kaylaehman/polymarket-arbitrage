"""Phase 4 — edge detection with CONFIDENCE-SCALED threshold.

Compares a projection's model probability against a market's implied price,
nets out fees + slippage, and gates the result. Confidence propagates here as
the non-negotiable rule: LOW confidence -> HIGHER required edge, never a
confident bet. Gates also require confidence above a floor, adequate liquidity,
and a sane time-to-resolution.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EdgeConfig:
    base_threshold: float = 0.05      # min net edge at full confidence
    threshold_conf_scale: float = 0.15  # extra edge demanded as confidence -> 0
    confidence_floor: float = 0.25    # below this, never act
    min_liquidity: float = 100.0      # $ liquidity floor
    min_days_to_resolution: float = 0.0
    max_days_to_resolution: float = 120.0
    fee: float = 0.02                 # round-trip fee estimate
    slippage: float = 0.01            # price slippage estimate


@dataclass(frozen=True)
class EdgeResult:
    side: str                 # "YES" | "NO" | "none"
    model_prob: float
    market_prob: float
    raw_edge: float           # signed: model - market
    net_edge: float           # |raw_edge| - fee - slippage
    threshold: float          # confidence-scaled requirement
    confidence: float
    passes: bool
    reasons: list             # why it passed/failed (explainable)


def required_threshold(confidence: float, cfg: EdgeConfig) -> float:
    """Edge required to act, scaled inversely with confidence."""
    return cfg.base_threshold + (1.0 - confidence) * cfg.threshold_conf_scale


def compute_edge(
    model_prob: float,
    confidence: float,
    market_prob: float,
    *,
    liquidity: float,
    days_to_resolution: float,
    cfg: EdgeConfig = EdgeConfig(),
) -> EdgeResult:
    raw = model_prob - market_prob               # + => YES underpriced
    net = abs(raw) - cfg.fee - cfg.slippage
    side = "YES" if raw > 0 else "NO"
    threshold = required_threshold(confidence, cfg)

    reasons: list = []
    if confidence < cfg.confidence_floor:
        reasons.append(f"confidence {confidence:.2f} < floor {cfg.confidence_floor}")
    if liquidity < cfg.min_liquidity:
        reasons.append(f"liquidity {liquidity:.0f} < min {cfg.min_liquidity:.0f}")
    if not (cfg.min_days_to_resolution <= days_to_resolution <= cfg.max_days_to_resolution):
        reasons.append(f"days_to_resolution {days_to_resolution:.1f} out of "
                       f"[{cfg.min_days_to_resolution}, {cfg.max_days_to_resolution}]")
    if net < threshold:
        reasons.append(f"net edge {net:.3f} < threshold {threshold:.3f}")

    passes = not reasons
    if passes:
        reasons.append(f"net edge {net:.3f} >= threshold {threshold:.3f} on {side}")
    return EdgeResult(
        side=side if passes else "none", model_prob=round(model_prob, 4),
        market_prob=round(market_prob, 4), raw_edge=round(raw, 4),
        net_edge=round(net, 4), threshold=round(threshold, 4),
        confidence=round(confidence, 4), passes=passes, reasons=reasons,
    )
