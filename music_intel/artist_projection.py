"""Annual top-Spotify-artist projection model (EXPLAINABLE HEURISTIC, not ground truth).

Projects P(artist = #1 for the full year) from each contender's current daily streaming
rate and 2026 release activity. All functions are pure (no I/O, no network). Coefficients
are module constants documented below — override them by passing `sharpness` or adjusting
the constants directly for tuning experiments.

Confidence propagation: a tight race -> LOW confidence -> WIDE probability band.
Downstream edge logic widens its threshold when confidence is low.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Coefficients (APPROXIMATE, tunable — NOT derived from any proprietary data)
# ---------------------------------------------------------------------------
ALBUM_BOOST: float = 0.08     # multiplicative lift per 2026 album release
RECENCY_BOOST: float = 0.10   # additional lift when latest release is within RECENCY_DAYS
RECENCY_DAYS: int = 120       # threshold for recency boost
FACTOR_CAP: float = 1.6       # maximum release_factor value
SHARPNESS: float = 6.0        # softmax temperature: higher = winner-takes-more


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


def release_factor(albums: int, days_since_latest: Optional[float]) -> float:
    """Compute the release-activity multiplier for projected streams.

    Args:
        albums: Number of albums released in 2026.
        days_since_latest: Days since the most recent release, or None if no release.

    Returns:
        Multiplier in [1.0, FACTOR_CAP].
    """
    factor = 1.0 + ALBUM_BOOST * albums
    if days_since_latest is not None and days_since_latest <= RECENCY_DAYS:
        factor += RECENCY_BOOST
    return min(factor, FACTOR_CAP)


def _build_projection(contender: dict, days_remaining: float, days_elapsed: float) -> tuple[float, float]:
    """Return (ytd, projected_streams) for one contender dict."""
    daily_rate = float(contender["daily_rate"])
    albums = int(contender.get("albums_2026", 0))
    days_since = contender.get("days_since_release", None)
    ytd_override = contender.get("ytd_estimate", None)

    ytd = float(ytd_override) if ytd_override is not None else daily_rate * days_elapsed
    rf = release_factor(albums, days_since)
    projected = ytd + daily_rate * days_remaining * rf
    return ytd, projected, rf


def project_top_artist(
    contenders: list,
    days_remaining: float,
    days_elapsed: float,
    sharpness: float = SHARPNESS,
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

    # Step 1: build (ytd, projected, rf) for every contender
    rows = []
    for c in contenders:
        ytd, projected, rf = _build_projection(c, days_remaining, days_elapsed)
        rows.append((c, ytd, projected, rf))

    # Step 2: softmax over projected values (guard M==0 -> uniform)
    max_proj = max(r[2] for r in rows)
    if max_proj == 0.0:
        raw = [1.0] * len(rows)
    else:
        raw = [math.exp(sharpness * (proj / max_proj - 1.0)) for _, _, proj, _ in rows]

    total = sum(raw)
    probs = [r / total for r in raw]

    # Step 3: confidence band — sort by projected descending
    sorted_proj = sorted((proj for _, _, proj, _ in rows), reverse=True)
    if len(sorted_proj) >= 2 and sorted_proj[0] > 0:
        confidence = max(0.0, min(1.0, (sorted_proj[0] - sorted_proj[1]) / sorted_proj[0]))
    else:
        confidence = 1.0

    # Step 4: assemble ArtistProjection objects; assign rank by projected desc order
    proj_order = sorted(range(len(rows)), key=lambda i: rows[i][2], reverse=True)
    rank_map = {i: rank + 1 for rank, i in enumerate(proj_order)}

    results = []
    for idx, ((c, ytd, projected, rf), p) in enumerate(zip(rows, probs)):
        half = (1.0 - confidence) * 0.5 * p
        prob_low = max(0.0, p - half)
        prob_high = min(1.0, p + half)

        drivers = [
            ("ytd", round(ytd, 4)),
            ("daily_rate", float(c["daily_rate"])),
            ("release_factor", round(rf, 4)),
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
