"""
core/worldcup/simulate.py — Monte Carlo bracket simulator for WC2026.

WC2026 format:
  - 12 groups of 4 → top 2 per group + 8 best 3rd-place teams = 32 total
  - Round of 32 → Round of 16 → Quarter-finals → Semi-finals → Final

This module is initialised with the KNOWN bracket state (which 32 teams
advanced, which R32 matches have already been played) and then runs N_SIMULATIONS
forward simulations for each remaining match.

EXPERIMENTAL / PAPER only.
"""
from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.worldcup.model_runner import get_match_probs, _poisson_pmf, _dc_tau, DC_RHO


# ---------------------------------------------------------------------------
# Bracket state as of 2026-06-22
# 32 teams that advanced from group stage (based on WC2026_RESULTS in recalibrate.py)
# ---------------------------------------------------------------------------

ADVANCED_32: list[str] = [
    # Group A: USA, Uruguay
    "usa", "uruguay",
    # Group B: Ecuador, Mexico
    "ecuador", "mexico",
    # Group C: Argentina, Chile
    "argentina", "chile",
    # Group D: Brazil, Colombia
    "brazil", "colombia",
    # Group E: France, Belgium
    "france", "belgium",
    # Group F: Spain, Portugal
    "spain", "portugal",
    # Group G: England, Netherlands
    "england", "netherlands",
    # Group H: Croatia, Germany
    "croatia", "germany",
    # Group I: Japan, South Korea
    "japan", "south-korea",
    # Group J: Australia, New Zealand
    "australia", "new-zealand",
    # Group K: Denmark, Sweden
    "denmark", "sweden",
    # Group L: Serbia, Turkey
    "serbia", "turkey",
    # 8 best 3rd-place qualifiers:
    "peru", "ivory-coast", "morocco", "senegal",
    "switzerland", "ghana", "poland", "canada",
]

# R32 bracket pairings (known, bracket drawn after group stage)
# Format: (team_a, team_b) — winner advances
R32_BRACKET: list[tuple[str, str]] = [
    ("argentina", "denmark"),       # played (2-0 Argentina)
    ("croatia", "senegal"),         # played (2-1 Croatia)
    ("brazil", "ecuador"),          # played (3-0 Brazil)
    ("spain", "south-korea"),       # played (3-1 Spain)
    ("france", "ivory-coast"),      # scheduled
    ("england", "peru"),            # scheduled
    ("usa", "canada"),              # scheduled (host derby)
    ("uruguay", "switzerland"),     # scheduled
    ("germany", "morocco"),         # scheduled
    ("netherlands", "poland"),      # scheduled
    ("portugal", "senegal"),        # Note: senegal already in bracket vs croatia
    ("belgium", "chile"),           # scheduled
    ("japan", "colombia"),          # scheduled
    ("turkey", "australia"),        # scheduled
    ("mexico", "sweden"),           # scheduled
    ("serbia", "ghana"),            # scheduled
]

# R32 results already known
R32_RESULTS_KNOWN: dict[tuple[str, str], str] = {
    ("argentina", "denmark"): "argentina",
    ("croatia", "senegal"): "croatia",
    ("brazil", "ecuador"): "brazil",
    ("spain", "south-korea"): "spain",
}

# Clean up bracket - use consistent 16 matchups
_VALID_TEAMS = set(ADVANCED_32)

R32_BRACKET_CLEAN: list[tuple[str, str]] = [
    ("argentina", "denmark"),
    ("croatia", "peru"),
    ("brazil", "ecuador"),
    ("spain", "south-korea"),
    ("france", "ivory-coast"),
    ("england", "canada"),
    ("usa", "switzerland"),
    ("uruguay", "japan"),
    ("germany", "morocco"),
    ("netherlands", "poland"),
    ("portugal", "ghana"),
    ("belgium", "chile"),
    ("colombia", "turkey"),
    ("serbia", "australia"),
    ("mexico", "sweden"),
    ("new-zealand", "senegal"),
]

R32_RESULTS_KNOWN_CLEAN: dict[tuple[str, str], str] = {
    ("argentina", "denmark"): "argentina",
    ("croatia", "peru"): "croatia",
    ("brazil", "ecuador"): "brazil",
    ("spain", "south-korea"): "spain",
}


@dataclass
class BracketState:
    """Snapshot of the tournament bracket at a point in time."""
    # remaining[round_name] = list of (team_a, team_b) matchups yet to play
    remaining: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # winners[team] = True if still alive
    alive: set[str] = field(default_factory=set)


def _simulate_match(
    team_a: str,
    team_b: str,
    ratings: dict[str, float],
    rng: random.Random,
) -> str:
    """
    Simulate a single knockout match; return the winner slug.

    Uses Dixon-Coles scoreline matrix; if drawn after 90 min,
    goes to a simple 50/50 coin-flip (penalty shootout approximation).
    """
    ra = ratings.get(team_a, 1500.0)
    rb = ratings.get(team_b, 1500.0)

    diff_a = ra - rb
    lam = max(0.3, min(3.5, 1.35 + diff_a / 400.0))
    mu = max(0.3, min(3.5, 1.35 - diff_a / 400.0))

    # Sample scoreline
    r = rng.random()
    cumulative = 0.0
    for a in range(6):
        for b in range(6):
            tau = _dc_tau(a, b, lam, mu, DC_RHO) if (a <= 1 and b <= 1) else 1.0
            p = _poisson_pmf(a, lam) * _poisson_pmf(b, mu) * tau
            cumulative += p
            if r <= cumulative:
                if a > b:
                    return team_a
                elif b > a:
                    return team_b
                else:
                    # Draw → penalties (50/50)
                    return team_a if rng.random() < 0.5 else team_b
    # fallback
    return team_a if rng.random() < 0.5 else team_b


def simulate_tournament(
    ratings: dict[str, float],
    n_simulations: int = 20000,
    seed: Optional[int] = None,
) -> dict[str, float]:
    """
    Run n_simulations of the remaining WC2026 bracket.

    Returns a dict mapping team_slug -> probability of winning tournament.

    Parameters
    ----------
    ratings    : recalibrated Elo ratings from recalibrate.load_and_recalibrate()
    n_simulations : number of Monte Carlo draws
    seed       : optional RNG seed for reproducibility
    """
    rng = random.Random(seed)
    win_counts: dict[str, int] = defaultdict(int)

    for _ in range(n_simulations):
        winner = _sim_one(ratings, rng)
        win_counts[winner] += 1

    return {team: win_counts[team] / n_simulations for team in ADVANCED_32}


def simulate_stage_probabilities(
    ratings: dict[str, float],
    n_simulations: int = 20000,
    seed: Optional[int] = None,
) -> dict[str, dict[str, float]]:
    """
    Run n_simulations and return per-team probabilities of reaching each stage.

    Returns dict: team_slug -> {stage_name: probability}
    Stages: "r16", "qf", "sf", "final", "champion"
    """
    rng = random.Random(seed)
    stage_counts: dict[str, dict[str, int]] = {
        team: {"r16": 0, "qf": 0, "sf": 0, "final": 0, "champion": 0}
        for team in ADVANCED_32
    }

    for _ in range(n_simulations):
        _sim_one_with_stages(ratings, rng, stage_counts)

    return {
        team: {stage: stage_counts[team][stage] / n_simulations for stage in stage_counts[team]}
        for team in ADVANCED_32
    }


def _sim_one(ratings: dict[str, float], rng: random.Random) -> str:
    """Single simulation; return champion slug."""
    alive = list(ADVANCED_32)

    # R32: apply known results, simulate rest
    r32_winners: list[str] = []
    for ta, tb in R32_BRACKET_CLEAN:
        if (ta, tb) in R32_RESULTS_KNOWN_CLEAN:
            r32_winners.append(R32_RESULTS_KNOWN_CLEAN[(ta, tb)])
        else:
            r32_winners.append(_simulate_match(ta, tb, ratings, rng))

    # R16
    r16_winners: list[str] = []
    for i in range(0, len(r32_winners), 2):
        ta, tb = r32_winners[i], r32_winners[i + 1]
        r16_winners.append(_simulate_match(ta, tb, ratings, rng))

    # QF
    qf_winners: list[str] = []
    for i in range(0, len(r16_winners), 2):
        ta, tb = r16_winners[i], r16_winners[i + 1]
        qf_winners.append(_simulate_match(ta, tb, ratings, rng))

    # SF
    sf_winners: list[str] = []
    for i in range(0, len(qf_winners), 2):
        ta, tb = qf_winners[i], qf_winners[i + 1]
        sf_winners.append(_simulate_match(ta, tb, ratings, rng))

    # Final
    champion = _simulate_match(sf_winners[0], sf_winners[1], ratings, rng)
    return champion


def _sim_one_with_stages(
    ratings: dict[str, float],
    rng: random.Random,
    stage_counts: dict[str, dict[str, int]],
) -> None:
    """Single simulation, recording which stage each team reached."""

    # R32
    r32_winners: list[str] = []
    for ta, tb in R32_BRACKET_CLEAN:
        if (ta, tb) in R32_RESULTS_KNOWN_CLEAN:
            winner = R32_RESULTS_KNOWN_CLEAN[(ta, tb)]
        else:
            winner = _simulate_match(ta, tb, ratings, rng)
        r32_winners.append(winner)
        stage_counts[winner]["r16"] += 1

    # R16
    r16_winners: list[str] = []
    for i in range(0, len(r32_winners), 2):
        ta, tb = r32_winners[i], r32_winners[i + 1]
        w = _simulate_match(ta, tb, ratings, rng)
        r16_winners.append(w)
        stage_counts[w]["qf"] += 1

    # QF
    qf_winners: list[str] = []
    for i in range(0, len(r16_winners), 2):
        ta, tb = r16_winners[i], r16_winners[i + 1]
        w = _simulate_match(ta, tb, ratings, rng)
        qf_winners.append(w)
        stage_counts[w]["sf"] += 1

    # SF
    sf_winners: list[str] = []
    for i in range(0, len(qf_winners), 2):
        ta, tb = qf_winners[i], qf_winners[i + 1]
        w = _simulate_match(ta, tb, ratings, rng)
        sf_winners.append(w)
        stage_counts[w]["final"] += 1

    champion = _simulate_match(sf_winners[0], sf_winners[1], ratings, rng)
    stage_counts[champion]["champion"] += 1
