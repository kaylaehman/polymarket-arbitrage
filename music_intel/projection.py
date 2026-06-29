"""Phase 2 — transparent chart-position projection (CALIBRATED HEURISTIC, not ground truth).

Billboard's exact equivalent-unit coefficients and tracking-week cutoffs are NOT
public, so this is an explainable heuristic — every projection emits a confidence
BAND and the drivers behind it, never a bare point bet. Coefficients are exposed
as constants (overridable via the music_intel config block).

Confidence propagation: a tight race or thin data -> LOW confidence -> WIDE band.
Downstream edge logic widens its threshold when confidence is low.

NOTE: this module must NEVER import music_intel.sources.billboard (Billboard
results are ground-truth/calibration only — no leakage into live inputs).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional

from music_intel.sources.base import ChartRecord

# --- Coefficients (APPROXIMATE; tunable via config — NOT public Billboard values) ---
DEFAULT_STREAM_EU = 1250.0    # paid on-demand streams per 1 chart unit (Hot 100 song-units approx)
DEFAULT_SALE_EU = 1.0         # 1 pure sale = 1 unit
DEFAULT_AIRPLAY_PER_UNIT = 0.0  # airplay impressions per unit (0 until an airplay feed is wired)
# Logistic steepness mapping the leader's unit-margin to P(#1).
DEFAULT_MARGIN_K = 12.0


@dataclass(frozen=True)
class Projection:
    chart: str
    as_of: date
    target: str                  # "artist - title"
    point_estimate_units: float  # projected weekly equivalent units for the target
    projected_rank: int          # 1 = projected #1 (0 if target absent from data)
    prob: float                  # P(market binary, e.g. target is #1)
    prob_low: float              # confidence band
    prob_high: float
    confidence: float            # 0..1
    drivers: list                # explainable [(name, value), ...]


def track_key(artist: str, title: str) -> str:
    return f"{(artist or '').strip()} - {(title or '').strip()}".lower()


def equivalent_units(
    rec: ChartRecord,
    *,
    stream_eu: float = DEFAULT_STREAM_EU,
    sale_eu: float = DEFAULT_SALE_EU,
    airplay_per_unit: float = DEFAULT_AIRPLAY_PER_UNIT,
) -> float:
    """Weekly equivalent units from a record. v1 is streaming-driven (kworb feed);
    sales/airplay terms are present but 0-weighted until those feeds are wired."""
    streams = rec.streams_7day or rec.streams_period or 0
    units = (streams / stream_eu) if stream_eu > 0 else 0.0
    # sales/airplay seams (kworb streaming source carries neither yet):
    # units += sales * sale_eu + airplay * airplay_per_unit
    return units


def project_number_one(
    records: list[ChartRecord],
    target_artist: str,
    target_title: str,
    *,
    stream_eu: float = DEFAULT_STREAM_EU,
    margin_k: float = DEFAULT_MARGIN_K,
    as_of: Optional[date] = None,
) -> Projection:
    """Project P(target is #1) from projected equivalent units across the field.

    P(#1) is a logistic of the leader's relative unit margin to the runner-up.
    Confidence scales with that margin AND field-data completeness; low either way
    -> low confidence -> wide band.
    """
    as_of = as_of or (records[0].as_of if records else date.today())
    tkey = track_key(target_artist, target_title)

    scored = sorted(
        ((track_key(r.artist, r.title), equivalent_units(r, stream_eu=stream_eu)) for r in records),
        key=lambda x: -x[1],
    )
    target_units = next((u for k, u in scored if k == tkey), 0.0)
    projected_rank = next((i + 1 for i, (k, _) in enumerate(scored) if k == tkey), 0)

    leader_units = scored[0][1] if scored else 0.0
    runner_units = scored[1][1] if len(scored) > 1 else 0.0
    # Margin of the TARGET vs the best *other* track (negative if target is behind).
    best_other = max((u for k, u in scored if k != tkey), default=0.0)
    denom = max(target_units, best_other, 1e-9)
    margin = (target_units - best_other) / denom  # in [-1, 1]

    prob = 1.0 / (1.0 + math.exp(-margin_k * margin))

    # Confidence: tight race (|margin| small) or thin field -> low confidence.
    field_factor = min(len(records) / 10.0, 1.0)         # need a decent field
    margin_factor = min(abs(margin) * 4.0, 1.0)          # decisive gap -> confident
    data_factor = 1.0 if target_units > 0 else 0.2       # target seen at all?
    confidence = round(field_factor * (0.4 + 0.6 * margin_factor) * data_factor, 4)

    half_band = (1.0 - confidence) * 0.5                 # wide band when unconfident
    prob_low = round(max(0.0, prob - half_band), 4)
    prob_high = round(min(1.0, prob + half_band), 4)

    drivers = [
        ("target_units", round(target_units, 2)),
        ("best_other_units", round(best_other, 2)),
        ("unit_margin", round(margin, 4)),
        ("field_size", len(records)),
        ("projected_rank", projected_rank),
    ]
    return Projection(
        chart=records[0].chart if records else "unknown", as_of=as_of,
        target=f"{target_artist} - {target_title}",
        point_estimate_units=round(target_units, 2), projected_rank=projected_rank,
        prob=round(prob, 4), prob_low=prob_low, prob_high=prob_high,
        confidence=confidence, drivers=drivers,
    )
