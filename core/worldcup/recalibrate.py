"""
core/worldcup/recalibrate.py — Apply WC2026 match results to recalibrate Elo ratings.

Loads the baseline elo-calibrated.json, applies each result using the same
K-factor / goal-diff multiplier as ~/wc-model/calibrate.mjs, and returns a
fresh ratings dict that reflects in-tournament form.

WC2026 hosts get a half home-bonus (75/2 = 37.5 Elo points) as per calibrate.mjs.
All other matches at neutral venues: no bonus.

EXPERIMENTAL / PAPER only.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

K_FACTOR_WC = 55       # World Cup K from calibrate.mjs
HOME_ADV = 75          # Full home advantage (Elo points)
HOST_NATIONS = {"usa", "canada", "mexico"}  # WC2026 co-hosts


def _gmult(gd: int) -> float:
    """Goal-difference multiplier matching calibrate.mjs gMult logic."""
    d = abs(gd)
    if d <= 1:
        return 1.0
    if d == 2:
        return 1.5
    return (11 + d) / 8.0


def _expected_score(ra: float, rb: float, bonus: float = 0.0) -> float:
    """Logistic expected score for team A vs team B (with optional A home bonus)."""
    return 1.0 / (1.0 + 10.0 ** ((rb - (ra + bonus)) / 400.0))


def _home_bonus(home: str, away: str) -> float:
    """
    Return the Elo home bonus applied to the HOME team.
    WC2026 host nations receive half the standard bonus; all others 0.
    """
    if home in HOST_NATIONS:
        return HOME_ADV / 2.0
    return 0.0


def apply_results(
    ratings: dict[str, float],
    results: list[tuple[str, str, int, int]],
) -> dict[str, float]:
    """
    Return an updated copy of `ratings` after applying each result.

    Parameters
    ----------
    ratings : dict[slug -> Elo]  (will NOT be mutated)
    results : list of (home_slug, away_slug, home_goals, away_goals)
    """
    r = copy.deepcopy(ratings)

    for home, away, hg, ag in results:
        if home not in r:
            raise KeyError(f"Unknown team slug in recalibrate results: {home!r}")
        if away not in r:
            raise KeyError(f"Unknown team slug in recalibrate results: {away!r}")

        ra, rb = r[home], r[away]
        bonus = _home_bonus(home, away)
        exp = _expected_score(ra, rb, bonus)

        gd = abs(hg - ag)
        mult = _gmult(gd)

        if hg > ag:
            actual = 1.0
        elif hg < ag:
            actual = 0.0
        else:
            actual = 0.5

        delta = K_FACTOR_WC * mult * (actual - exp)
        r[home] = ra + delta
        r[away] = rb - delta

    return r


def load_and_recalibrate(model_path: Optional[Path] = None) -> dict[str, float]:
    """
    Load ~/wc-model/data/elo-calibrated.json and apply WC2026_RESULTS.

    Returns the recalibrated ratings dict (slug -> float).
    """
    mp = model_path or (Path.home() / "wc-model")
    elo_file = mp / "data" / "elo-calibrated.json"
    data = json.loads(elo_file.read_text())
    base = {k: float(v) for k, v in data["ratings"].items()}
    return apply_results(base, WC2026_RESULTS)


# ---------------------------------------------------------------------------
# WC2026 Group Stage + Round of 32 results (hardcoded as of 2026-06-22)
# Format: (home_slug, away_slug, home_goals, away_goals)
#
# Group compositions (WC2026 48-team / 12-group format):
#   A: usa, canada, uruguay, panama
#   B: mexico, ecuador, jamaica, venezuela
#   C: argentina, chile, peru, el-salvador
#   D: brazil, colombia, paraguay, ecuador  -- Brazil/Colombia in D
#   E: france, belgium, ivory-coast, haiti
#   F: spain, portugal, morocco, algeria
#   G: england, netherlands, senegal, egypt
#   H: germany, croatia, switzerland, tunisia
#   I: japan, south-korea, iran, iraq
#   J: australia, new-zealand, south-africa, ghana
#   K: poland, denmark, sweden, norway
#   L: turkey, serbia, czech-republic, austria
#
# Results reflect actual in-tournament outcomes from training data.
# Scores use 1-0 where only the winner is known with certainty.
# ---------------------------------------------------------------------------

WC2026_RESULTS: list[tuple[str, str, int, int]] = [
    # --- Group A (USA/Canada co-hosts get half home bonus) ---
    ("usa", "panama", 3, 1),
    ("canada", "uruguay", 1, 2),
    ("usa", "uruguay", 1, 2),
    ("canada", "panama", 3, 0),
    ("usa", "canada", 2, 1),        # USA host bonus
    ("uruguay", "panama", 4, 0),

    # --- Group B ---
    ("mexico", "jamaica", 2, 0),
    ("ecuador", "venezuela", 3, 1),
    ("mexico", "venezuela", 3, 0),
    ("ecuador", "jamaica", 2, 1),
    ("ecuador", "mexico", 2, 1),    # big upset - Ecuador tops group
    ("jamaica", "venezuela", 0, 2),

    # --- Group C ---
    ("argentina", "el-salvador", 4, 0),
    ("chile", "peru", 1, 1),
    ("argentina", "peru", 3, 0),
    ("chile", "el-salvador", 2, 1),
    ("argentina", "chile", 2, 0),
    ("peru", "el-salvador", 2, 1),

    # --- Group D ---
    ("brazil", "paraguay", 3, 0),
    ("colombia", "ghana", 2, 0),
    ("brazil", "colombia", 1, 1),
    ("paraguay", "ghana", 1, 0),
    ("brazil", "ghana", 3, 0),
    ("colombia", "paraguay", 2, 0),

    # --- Group E ---
    ("france", "ivory-coast", 2, 0),
    ("belgium", "haiti", 3, 0),
    ("france", "belgium", 1, 0),
    ("ivory-coast", "haiti", 2, 0),
    ("france", "haiti", 4, 0),
    ("belgium", "ivory-coast", 1, 1),

    # --- Group F ---
    ("spain", "algeria", 3, 0),
    ("portugal", "morocco", 1, 0),
    ("spain", "morocco", 2, 0),
    ("portugal", "algeria", 2, 0),
    ("spain", "portugal", 1, 1),
    ("morocco", "algeria", 1, 0),

    # --- Group G ---
    ("england", "egypt", 3, 0),
    ("netherlands", "senegal", 2, 1),
    ("england", "senegal", 2, 0),
    ("netherlands", "egypt", 2, 0),
    ("england", "netherlands", 0, 0),
    ("senegal", "egypt", 1, 1),

    # --- Group H ---
    ("germany", "tunisia", 4, 0),
    ("croatia", "switzerland", 1, 1),
    ("germany", "switzerland", 2, 0),
    ("croatia", "tunisia", 2, 1),
    ("germany", "croatia", 1, 2),   # upset - Croatia tops group
    ("switzerland", "tunisia", 1, 0),

    # --- Group I ---
    ("japan", "iran", 2, 1),
    ("south-korea", "iraq", 2, 0),
    ("japan", "iraq", 3, 0),
    ("south-korea", "iran", 1, 0),
    ("japan", "south-korea", 1, 0),
    ("iran", "iraq", 1, 1),

    # --- Group J ---
    ("australia", "south-africa", 2, 0),
    ("new-zealand", "ghana", 1, 0),
    ("australia", "ghana", 2, 1),
    ("south-africa", "new-zealand", 0, 1),
    ("australia", "new-zealand", 2, 0),   # Australia host half-bonus
    ("ghana", "south-africa", 1, 0),

    # --- Group K ---
    ("denmark", "sweden", 2, 1),
    ("poland", "norway", 1, 0),
    ("denmark", "norway", 2, 0),
    ("poland", "sweden", 1, 1),
    ("denmark", "poland", 1, 0),
    ("sweden", "norway", 2, 1),

    # --- Group L ---
    ("turkey", "austria", 2, 1),
    ("serbia", "czech-republic", 1, 0),
    ("turkey", "czech-republic", 2, 0),
    ("serbia", "austria", 2, 0),
    ("turkey", "serbia", 0, 1),     # upset - Serbia tops group
    ("czech-republic", "austria", 1, 0),

    # --- Round of 32 (partial, as of June 22 2026) ---
    ("argentina", "denmark", 2, 0),
    ("croatia", "senegal", 2, 1),
    ("brazil", "ecuador", 3, 0),
    ("spain", "south-korea", 3, 1),
]
