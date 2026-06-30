"""Annual top-Spotify-artist projection model (EXPLAINABLE HEURISTIC, not ground truth).

Projects P(artist = #1 for the full year) from each contender's current daily streaming
rate. Release activity is modelled as UNCERTAINTY (wider probability band), NOT as a rate
multiplier — the current daily_rate already reflects any recent album activity.

All functions are pure (no I/O, no network). Coefficients are module constants documented
below — override them by passing `sharpness` or adjusting the constants directly for
tuning experiments.

Confidence propagation: a tight race -> LOW confidence -> WIDE probability band.
Release volatility: a recent/active releaser has an even wider band on top of that.
Downstream edge logic widens its threshold when confidence is low.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Coefficients (APPROXIMATE, tunable — NOT derived from any proprietary data)
# ---------------------------------------------------------------------------
RECENCY_DAYS: int = 120       # threshold for recency volatility bump
RECENCY_VOL: float = 0.5      # extra volatility when latest release is within RECENCY_DAYS
VOL_PER_ALBUM: float = 0.1    # per-2026-album volatility increment
VOL_CAP: float = 2.0          # maximum release_volatility value
SHARPNESS: float = 6.0        # softmax temperature: higher = winner-takes-more

# Catalog-maturity weighting of the FORWARD projection (Task A).
# maturity_days = catalog_total / daily_rate ≈ how many days of streaming the whole
# all-time catalog represents. A deep catalog (>= MATURITY_REF_DAYS) keeps its full
# forward rate; a spike-concentrated newcomer is discounted toward MATURITY_FLOOR.
# MATURITY_LAMBDA in [0,1] scales how strongly the discount applies (0 = off).
MATURITY_REF_DAYS: float = 365.0
MATURITY_FLOOR: float = 0.5
# Default OFF. Validated against 2021/2022/2024 Wayback history: maturity weighting
# did NOT fix the 2021 miss (kworb's mid-year global stream-delta had Olivia Rodrigo
# ~1.5B ahead of Bad Bunny, who nonetheless won Spotify Wrapped — a data-availability
# gap, not a forward-rate one) and left 2024 unchanged at 4/4. Kept as an opt-in
# tuning knob (it's sound on richer data) but not enabled, since it earned no measured
# backtest improvement.
MATURITY_LAMBDA: float = 0.0


@dataclass(frozen=True)
class ArtistProjection:
    """Projection result for one contender."""
    name: str
    projected_units: float   # projected full-year streams (millions)
    prob: float              # P(this artist is #1)
    prob_low: float
    prob_high: float
    confidence: float
    drivers: list            # [(name, value), ...]


def release_volatility(albums: int, days_since_latest: Optional[float]) -> float:
    """How uncertain an artist's forward streaming is.

    A recent/active releaser has a less predictable trajectory (fresh albums spike
    then decay), so its projection deserves a WIDER band. >= 1.0; never boosts the
    point estimate.

    Args:
        albums: Number of albums released in 2026.
        days_since_latest: Days since the most recent release, or None if no release.

    Returns:
        Volatility multiplier in [1.0, VOL_CAP].
    """
    v = 1.0
    if days_since_latest is not None and days_since_latest <= RECENCY_DAYS:
        v += RECENCY_VOL
    v += VOL_PER_ALBUM * albums
    return min(v, VOL_CAP)


def catalog_maturity_weight(
    catalog_total,
    daily_rate,
    *,
    lam: float = MATURITY_LAMBDA,
    ref_days: float = MATURITY_REF_DAYS,
    floor: float = MATURITY_FLOOR,
) -> float:
    """Forward-rate multiplier in [floor, 1.0] based on catalog maturity.

    maturity_days = catalog_total / daily_rate (≈ catalog age in days). A deep
    catalog (>= ref_days) returns ~1.0; a spike-concentrated newcomer returns
    toward `floor`. `lam` blends between 1.0 (off) and the maturity factor.

    Returns 1.0 (no effect) when lam<=0, catalog_total is missing/<=0, or
    daily_rate<=0 — never penalises on bad/absent data.
    """
    if lam <= 0.0 or not catalog_total or catalog_total <= 0 or daily_rate <= 0:
        return 1.0
    maturity_days = float(catalog_total) / float(daily_rate)
    factor = floor + (1.0 - floor) * min(1.0, maturity_days / ref_days)
    return 1.0 * (1.0 - lam) + factor * lam


def _build_projection(
    contender: dict,
    days_remaining: float,
    days_elapsed: float,
    maturity_lambda: float = MATURITY_LAMBDA,
) -> tuple[float, float, float]:
    """Return (ytd, projected_streams, volatility) for one contender dict."""
    daily_rate = float(contender["daily_rate"])
    albums = int(contender.get("albums_2026", 0))
    days_since = contender.get("days_since_release", None)
    ytd_override = contender.get("ytd_estimate", None)

    ytd = float(ytd_override) if ytd_override is not None else daily_rate * days_elapsed
    weight = catalog_maturity_weight(
        contender.get("catalog_total"), daily_rate, lam=maturity_lambda
    )
    projected = ytd + daily_rate * days_remaining * weight
    vol = release_volatility(albums, days_since)
    return ytd, projected, vol


def project_top_artist(
    contenders: list,
    days_remaining: float,
    days_elapsed: float,
    sharpness: float = SHARPNESS,
    maturity_lambda: float = MATURITY_LAMBDA,
) -> list[ArtistProjection]:
    """Project P(#1) for each contender and return results sorted by prob descending.

    Args:
        contenders: List of dicts with keys:
            - name (str)
            - daily_rate (float): current daily streaming rate in millions
            - albums_2026 (int, default 0): albums released so far in 2026
            - days_since_release (float|None, default None): days since latest release
            - ytd_estimate (float|None, default None): override for YTD streams
        days_remaining: Days left in the year from today.
        days_elapsed: Days elapsed in the year so far.
        sharpness: Softmax temperature (higher = winner-takes-more).

    Returns:
        List of ArtistProjection sorted by prob descending. Probs sum to ~1.0.
    """
    if not contenders:
        return []

    # Step 1: build (ytd, projected, volatility) for every contender
    rows = []
    for c in contenders:
        ytd, projected, vol = _build_projection(c, days_remaining, days_elapsed, maturity_lambda)
        rows.append((c, ytd, projected, vol))

    # Step 2: softmax over projected values (guard M==0 -> uniform)
    max_proj = max(r[2] for r in rows)
    if max_proj == 0.0:
        raw = [1.0] * len(rows)
    else:
        raw = [math.exp(sharpness * (proj / max_proj - 1.0)) for _, _, proj, _ in rows]

    total = sum(raw)
    probs = [r / total for r in raw]

    # Step 3: confidence from top-2 gap ratio
    sorted_proj = sorted((proj for _, _, proj, _ in rows), reverse=True)
    if len(sorted_proj) >= 2 and sorted_proj[0] > 0:
        confidence = max(0.0, min(1.0, (sorted_proj[0] - sorted_proj[1]) / sorted_proj[0]))
    else:
        confidence = 1.0

    # Step 4: assemble ArtistProjection objects; assign rank by projected desc order
    proj_order = sorted(range(len(rows)), key=lambda i: rows[i][2], reverse=True)
    rank_map = {i: rank + 1 for rank, i in enumerate(proj_order)}

    results = []
    for idx, ((c, ytd, projected, vol), p) in enumerate(zip(rows, probs)):
        # Band widened by BOTH low-confidence AND release volatility; point estimate unchanged
        half = (1.0 - confidence) * 0.5 * p * vol
        prob_low = max(0.0, p - half)
        prob_high = min(1.0, p + half)

        drivers = [
            ("ytd", round(ytd, 4)),
            ("daily_rate", float(c["daily_rate"])),
            ("release_volatility", round(vol, 4)),
            ("projected_units", round(projected, 4)),
            ("rank", rank_map[idx]),
        ]
        results.append(ArtistProjection(
            name=c["name"],
            projected_units=round(projected, 4),
            prob=round(p, 8),
            prob_low=round(prob_low, 8),
            prob_high=round(prob_high, 8),
            confidence=round(confidence, 8),
            drivers=drivers,
        ))

    results.sort(key=lambda r: r.prob, reverse=True)
    return results


def rank_probabilities(
    contenders: list,
    days_remaining: float,
    days_elapsed: float,
    *,
    n_sims: int = 4000,
    seed: int = 12345,
) -> dict:
    """Monte-Carlo P(rank=k) per artist.

    Returns {name: {rank:int -> prob:float}} (rank 1-based).
    Only the FUTURE part of the projection is uncertain (YTD is known).

    Args:
        contenders: Same dict format as project_top_artist.
        days_remaining: Days left in the year.
        days_elapsed: Days elapsed in the year.
        n_sims: Number of Monte-Carlo simulations.
        seed: RNG seed for determinism.

    Returns:
        Dict mapping artist name to {rank -> probability} where ranks are 1-based.
        Empty dict if contenders is empty.
    """
    if not contenders:
        return {}

    # Pre-compute (mean, std) per contender
    params: list[tuple[str, float, float]] = []
    for c in contenders:
        daily_rate = float(c["daily_rate"])
        albums = int(c.get("albums_2026", 0))
        days_since = c.get("days_since_release", None)
        ytd_override = c.get("ytd_estimate", None)

        ytd = float(ytd_override) if ytd_override is not None else daily_rate * days_elapsed
        future = daily_rate * days_remaining
        mean = ytd + future
        vol = release_volatility(albums, days_since)
        std = future * 0.25 * vol
        params.append((c["name"], mean, std))

    # Tally rank counts across simulations
    counts: dict[str, dict[int, int]] = {name: {} for name, _, _ in params}
    rng = random.Random(seed)
    n = len(params)

    for _ in range(n_sims):
        samples = [max(0.0, rng.gauss(mean, std)) for _, mean, std in params]
        # argsort descending: highest sample -> rank 1
        order = sorted(range(n), key=lambda i: samples[i], reverse=True)
        for rank_idx, artist_idx in enumerate(order):
            rank = rank_idx + 1
            name = params[artist_idx][0]
            counts[name][rank] = counts[name].get(rank, 0) + 1

    return {
        name: {rank: cnt / n_sims for rank, cnt in rank_counts.items()}
        for name, rank_counts in counts.items()
    }
