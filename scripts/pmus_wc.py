"""
scripts/pmus_wc.py — Parse PM.US World Cup market slugs → (team, outcome_type, outcome_detail).

EXPERIMENTAL / PAPER only.  Does not execute any trades.

Slug taxonomy observed from live PM.US data (2026-06-23):
  tec-f-wc-2026-07-19-winner-{team3}          → tournament_winner / team
  tec-f-wc-2026-07-19-group{X}-winner-{t3}   → group_winner / group + team
  aqc-fifa-wc-2026-07-19-stgelim-{t3}-{stage} → stage_of_elimination / team + stage
  aachc-fifa-wc-2026-07-19-{t3}-gs-{player}  → goal_scorer / team + player
  aachc-fifa-wc-2026-06-27-gsgoal-{t3}-{n}g  → group_stage_goals / team + threshold
  aachc-fifa-wc-2026-07-19-tgs-{n}goa        → total_goals / threshold
  aachc-fifa-wc-2026-07-19-tps-{n}ps         → total_penalties / threshold
  aachc-fifa-wc-2026-07-19-ht-{n}ht          → hat_tricks / threshold
  aachc-fifa-wc-2026-07-19-gc-{p1}-{p2}-{n}g → player_combo_goals / players + threshold
  aachc-fifa-wc-2026-06-27-grouphiscore-{g}  → group_most_goals / group
  aachc-fifa-wc-2026-06-27-grouploscore-{g}  → group_fewest_goals / group
  aachc-fifa-wc-2026-07-19-topscorer-{p}     → top_scorer / player
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# PM.US 3-letter team abbreviation → Hicruben model slug mapping.
# Derived from marketSides[].team.abbreviation in live API data.
ABBREV_TO_SLUG: dict[str, str] = {
    "esp": "spain",
    "fra": "france",
    "eng": "england",
    "arg": "argentina",
    "bra": "brazil",
    "por": "portugal",
    "ger": "germany",
    "ned": "netherlands",
    "nor": "norway",
    "usa": "usa",
    "mex": "mexico",
    "aus": "australia",
    "jpn": "japan",
    "kor": "south-korea",
    "mar": "morocco",
    "col": "colombia",
    "uru": "uruguay",
    "cro": "croatia",
    "bel": "belgium",
    "sui": "switzerland",
    "den": "denmark",
    "pol": "poland",
    "tur": "turkey",
    "egy": "egypt",
    "sen": "senegal",
    "gha": "ghana",
    "can": "canada",
    "swe": "sweden",
    "nzl": "new-zealand",
    "cpv": "cape-verde",
    "irq": "iraq",
    "qat": "qatar",
    "ksa": "saudi-arabia",
    "pan": "panama",
    "ecu": "ecuador",
    "mli": "mali",
    "scot": "scotland",
    "sco": "scotland",
    "cze": "czech-republic",
    "zaf": "south-africa",
    "bih": "bosnia-and-herzegovina",
    "aut": "austria",
    "uzb": "uzbekistan",
    "par": "paraguay",
    "ivo": "ivory-coast",
    "civ": "ivory-coast",
    "hai": "haiti",
    "tun": "tunisia",
    "cod": "dr-congo",
    "jor": "jordan",
    "alg": "algeria",
    "ita": "italy",
    "nig": "nigeria",
    "jam": "jamaica",
    "ven": "venezuela",
    "per": "peru",
    "chi": "chile",
    "srb": "serbia",
    "cam": "cameroon",
    "wal": "wales",
    "gua": "guatemala",
    "hon": "honduras",
    "els": "el-salvador",
    "tri": "trinidad-and-tobago",
    "cru": "curacao",
}

# Stage codes from stgelim slugs
STAGE_CODES: dict[str, str] = {
    "r48": "group_stage",
    "r32": "round_of_32",
    "r16": "round_of_16",
    "qf": "quarter_final",
    "sf": "semi_final",
    "final": "final",
    "champ": "champion",
}


@dataclass
class WCMarketKey:
    """Structured key for a PM.US WC market."""

    slug: str
    outcome_type: str  # tournament_winner | group_winner | stage_of_elimination | ...
    team_slug: Optional[str] = None  # Hicruben model key (e.g. "spain")
    team_abbrev: Optional[str] = None  # raw 3-letter abbrev from PM.US
    stage: Optional[str] = None  # for stgelim markets
    group: Optional[str] = None  # for group_winner
    threshold: Optional[str] = None  # for goals/totals markets
    player: Optional[str] = None  # for goal_scorer / top_scorer
    raw_detail: str = ""


def parse_slug(slug: str) -> WCMarketKey:
    """
    Parse a PM.US WC market slug into a structured WCMarketKey.

    Returns outcome_type="unknown" for slugs that don't match known patterns.
    """
    s = slug.lower()

    # --- Tournament winner: tec-f-wc-2026-07-19-winner-{t3} ---
    m = re.match(r"tec-f-wc-\d{4}-\d{2}-\d{2}-winner-([a-z]{2,4})$", s)
    if m:
        abbrev = m.group(1)
        return WCMarketKey(
            slug=slug,
            outcome_type="tournament_winner",
            team_abbrev=abbrev,
            team_slug=ABBREV_TO_SLUG.get(abbrev),
            raw_detail=abbrev,
        )

    # --- Group winner: tec-f-wc-2026-07-19-group{X}-winner-{t3} ---
    m = re.match(r"tec-f-wc-\d{4}-\d{2}-\d{2}-group([a-l])-winner-([a-z]{2,4})$", s)
    if m:
        grp, abbrev = m.group(1), m.group(2)
        return WCMarketKey(
            slug=slug,
            outcome_type="group_winner",
            group=f"group_{grp}",
            team_abbrev=abbrev,
            team_slug=ABBREV_TO_SLUG.get(abbrev),
            raw_detail=f"group_{grp}-{abbrev}",
        )

    # --- Stage of elimination: aqc-fifa-wc-...-stgelim-{t3}-{stage} ---
    m = re.match(r"aqc-fifa-wc-\d{4}-\d{2}-\d{2}-stgelim-([a-z]{2,4})-([a-z0-9]+)$", s)
    if m:
        abbrev, stage_code = m.group(1), m.group(2)
        return WCMarketKey(
            slug=slug,
            outcome_type="stage_of_elimination",
            team_abbrev=abbrev,
            team_slug=ABBREV_TO_SLUG.get(abbrev),
            stage=STAGE_CODES.get(stage_code, stage_code),
            raw_detail=f"{abbrev}-{stage_code}",
        )

    # --- Goal scorer: aachc-fifa-wc-...-{t3}-gs-{player} ---
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-([a-z]{2,4})-gs-(.+)$", s)
    if m:
        abbrev, player = m.group(1), m.group(2)
        return WCMarketKey(
            slug=slug,
            outcome_type="goal_scorer",
            team_abbrev=abbrev,
            team_slug=ABBREV_TO_SLUG.get(abbrev),
            player=player,
            raw_detail=f"{abbrev}/{player}",
        )

    # --- Group stage goals: aachc-fifa-wc-...-gsgoal-{t3}-{n}g ---
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-gsgoal-([a-z]{2,4})-(\d+)g$", s)
    if m:
        abbrev, n = m.group(1), m.group(2)
        return WCMarketKey(
            slug=slug,
            outcome_type="group_stage_goals",
            team_abbrev=abbrev,
            team_slug=ABBREV_TO_SLUG.get(abbrev),
            threshold=n,
            raw_detail=f"{abbrev}-{n}g",
        )

    # --- Group most/fewest goals ---
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-grouphiscore-(group[a-l])$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="group_most_goals", group=m.group(1), raw_detail=m.group(1))
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-grouploscore-(group[a-l])$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="group_fewest_goals", group=m.group(1), raw_detail=m.group(1))

    # --- Total tournament goals ---
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-tgs-(\d+)goa$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="total_goals", threshold=m.group(1), raw_detail=m.group(1))

    # --- Total penalties / hat-tricks ---
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-tps-(\d+)ps$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="total_penalties", threshold=m.group(1), raw_detail=m.group(1))
    m = re.match(r"aachc-fifa-wc-\d{4}-\d{2}-\d{2}-ht-(\d+)ht$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="hat_tricks", threshold=m.group(1), raw_detail=m.group(1))

    # --- Top scorer ---
    m = re.match(r"tec-f-wc-\d{4}-\d{2}-\d{2}-topscorer-(.+)$", s)
    if m:
        return WCMarketKey(slug=slug, outcome_type="top_scorer", player=m.group(1), raw_detail=m.group(1))

    return WCMarketKey(slug=slug, outcome_type="unknown", raw_detail=s)


# ---------------------------------------------------------------------------
# Markets that our model CAN price
# ---------------------------------------------------------------------------

MODEL_PRICEABLE_TYPES = frozenset({
    "tournament_winner",
    "group_winner",
    "stage_of_elimination",
})


def is_model_priceable(key: WCMarketKey) -> bool:
    """
    True if we have a principled model probability for this market type.

    tournament_winner and group_winner require running the full Monte Carlo
    from cup26matches.com/data/probabilities.json (or re-implementing it here).
    stage_of_elimination can be approximated from advancement probs.

    Exact-score and goal-scorer markets are NOT model-priceable from Elo alone.
    """
    return key.outcome_type in MODEL_PRICEABLE_TYPES and key.team_slug is not None
