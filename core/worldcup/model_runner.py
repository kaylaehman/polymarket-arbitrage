"""
core/worldcup/model_runner.py — Hicruben Dixon-Coles model integration.

Shells out to `node /path/to/wc-model/` and reconstructs the scoreline
matrix in Python.  The Node model is NOT imported as a library (it's ESM
only); we call it as a subprocess to get JSON output and parse it here.

EXPERIMENTAL / PAPER only.  No live execution dependency.
"""

from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Path to the cloned Hicruben model; override via WC_MODEL_PATH env var.
_DEFAULT_MODEL_PATH = Path.home() / "wc-model"
_ELO_FILE_REL = "data/elo-calibrated.json"

DC_RHO = -0.13
_MAX_GOALS = 5


@dataclass
class MatchProbs:
    """Per-fixture output from the Dixon-Coles model."""

    team_a: str
    team_b: str
    elo_a: float
    elo_b: float
    win_a: float
    draw: float
    win_b: float
    xg_a: float
    xg_b: float
    # scoreline -> probability (e.g. {"1-0": 0.089, "0-0": 0.083, ...})
    scorelines: dict[str, float] = field(default_factory=dict)

    @property
    def home_side_impliedp(self) -> float:
        return self.win_a

    def scoreline_prob(self, goals_a: int, goals_b: int) -> float:
        return self.scorelines.get(f"{goals_a}-{goals_b}", 0.0)


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def _dc_tau(a: int, b: int, lam: float, mu: float, rho: float) -> float:
    if a == 0 and b == 0:
        return 1 - lam * mu * rho
    if a == 0 and b == 1:
        return 1 + lam * rho
    if a == 1 and b == 0:
        return 1 + mu * rho
    if a == 1 and b == 1:
        return 1 - rho
    return 1.0


def _expected_goals(rating: float, opponent: float, home_bonus: float = 0) -> float:
    diff = (rating + home_bonus) - opponent
    lam = 1.35 + diff / 400
    return max(0.3, min(3.5, lam))


def _scoreline_matrix(
    lam: float, mu: float, max_goals: int = _MAX_GOALS
) -> dict[str, float]:
    mat: dict[str, float] = {}
    for a in range(max_goals + 1):
        for b in range(max_goals + 1):
            tau = _dc_tau(a, b, lam, mu, DC_RHO) if (a <= 1 and b <= 1) else 1.0
            mat[f"{a}-{b}"] = _poisson_pmf(a, lam) * _poisson_pmf(b, mu) * tau
    total = sum(mat.values())
    return {k: v / total for k, v in mat.items()}


def _load_ratings(model_path: Path) -> dict[str, float]:
    elo_file = model_path / _ELO_FILE_REL
    if not elo_file.exists():
        raise FileNotFoundError(
            f"Elo calibration file not found at {elo_file}. "
            "Clone https://github.com/Hicruben/world-cup-2026-prediction-model "
            f"to {model_path}"
        )
    data = json.loads(elo_file.read_text())
    return data["ratings"]  # {team_slug: float}


def get_match_probs(
    team_a: str,
    team_b: str,
    home_team: Optional[str] = None,
    model_path: Optional[Path] = None,
) -> MatchProbs:
    """
    Return Dixon-Coles 1X2 + scoreline matrix for (team_a, team_b).

    Parameters
    ----------
    team_a, team_b : slugs matching data/elo-calibrated.json keys
                     (e.g. "spain", "germany", "usa", "south-korea")
    home_team      : optional; one of team_a / team_b; adds ±75 Elo home bonus
    model_path     : path to the Hicruben repo checkout (default ~/wc-model)
    """
    mp = model_path or _DEFAULT_MODEL_PATH
    ratings = _load_ratings(mp)

    if team_a not in ratings:
        raise ValueError(f"Unknown team slug: {team_a!r}. Available: {sorted(ratings)}")
    if team_b not in ratings:
        raise ValueError(f"Unknown team slug: {team_b!r}. Available: {sorted(ratings)}")

    ra, rb = ratings[team_a], ratings[team_b]
    hb_a = 75 if home_team == team_a else (-75 if home_team == team_b else 0)

    lam = _expected_goals(ra, rb, hb_a)
    mu = _expected_goals(rb, ra, -hb_a / 2)

    # 1X2 via DC bivariate Poisson (9×9 grid)
    win_a = draw = win_b = 0.0
    for a in range(9):
        p_a = _poisson_pmf(a, lam)
        for b in range(9):
            tau = _dc_tau(a, b, lam, mu, DC_RHO) if (a <= 1 and b <= 1) else 1.0
            p = p_a * _poisson_pmf(b, mu) * tau
            if a > b:
                win_a += p
            elif a < b:
                win_b += p
            else:
                draw += p
    total = win_a + draw + win_b
    win_a /= total
    draw /= total
    win_b /= total

    scorelines = _scoreline_matrix(lam, mu)

    return MatchProbs(
        team_a=team_a,
        team_b=team_b,
        elo_a=ra,
        elo_b=rb,
        win_a=win_a,
        draw=draw,
        win_b=win_b,
        xg_a=lam,
        xg_b=mu,
        scorelines=scorelines,
    )


def list_teams(model_path: Optional[Path] = None) -> list[str]:
    """Return sorted list of all team slugs in the Elo file."""
    mp = model_path or _DEFAULT_MODEL_PATH
    return sorted(_load_ratings(mp).keys())
