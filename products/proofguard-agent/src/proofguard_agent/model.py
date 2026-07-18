"""Deterministic, explainable in-play match-result model.

This is the signal source behind ProofGuard's edge. It is intentionally *not*
machine-learned: it is a transparent in-play Poisson model that any judge can
re-derive by hand. Given a pre-match prior over {HOME, DRAW, AWAY}, the current
score, and the match minute, it returns a fair probability distribution over the
final result by convolving the *remaining* expected goals (a shrinking Poisson
as time elapses) on top of the goals already scored.

Properties:
    * minute 0, 0-0            -> close to the pre-match prior ordering;
    * a goal                   -> immediate, sizeable shift toward the scorer;
    * time running out         -> probabilities converge on the current leader;
    * full time                -> the current result has probability ~1.

Pure standard library (``math`` only), fully deterministic, no external calls.
"""

from __future__ import annotations

import math
from typing import Any

RESULT_SELECTIONS = ("HOME", "DRAW", "AWAY")
_EPS = 1e-9


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def _normalized_prior(prior: dict[str, float] | None) -> dict[str, float]:
    values = {sel: max(_EPS, float((prior or {}).get(sel, 0.0))) for sel in RESULT_SELECTIONS}
    total = sum(values.values())
    if total <= 0.0:
        return {sel: 1.0 / 3.0 for sel in RESULT_SELECTIONS}
    return {sel: value / total for sel, value in values.items()}


def prematch_goal_rates(
    prior: dict[str, float] | None,
    *,
    base_total_goals: float = 2.7,
    supremacy_scale: float = 2.2,
) -> tuple[float, float]:
    """Map a pre-match result prior to per-side expected goals for a full match.

    Home supremacy (in goals) is proportional to the prior win-probability gap;
    the two rates always sum to ``base_total_goals`` and stay strictly positive.
    """

    p = _normalized_prior(prior)
    supremacy = supremacy_scale * (p["HOME"] - p["AWAY"])
    lam_home = max(0.05, (base_total_goals + supremacy) / 2.0)
    lam_away = max(0.05, (base_total_goals - supremacy) / 2.0)
    return lam_home, lam_away


def match_result_probabilities(
    prior: dict[str, float] | None,
    *,
    minute: int = 0,
    home_score: int = 0,
    away_score: int = 0,
    match_length: int = 90,
    base_total_goals: float = 2.7,
    supremacy_scale: float = 2.2,
    max_goals: int = 10,
) -> dict[str, float]:
    """Fair {HOME, DRAW, AWAY} probabilities for the *final* result, in-play.

    Deterministic. Result is normalized and clamped strictly inside (0, 1) so it
    is directly usable as a MarketEvent model probability.
    """

    minute = max(0, min(match_length, int(minute)))
    remaining_fraction = max(0.0, (match_length - minute) / match_length)
    lam_home_pm, lam_away_pm = prematch_goal_rates(
        prior, base_total_goals=base_total_goals, supremacy_scale=supremacy_scale
    )
    mu_home = lam_home_pm * remaining_fraction
    mu_away = lam_away_pm * remaining_fraction
    current_diff = int(home_score) - int(away_score)

    p_home = p_draw = p_away = 0.0
    home_pmf = [_poisson_pmf(i, mu_home) for i in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(j, mu_away) for j in range(max_goals + 1)]
    for i, pi in enumerate(home_pmf):
        for j, pj in enumerate(away_pmf):
            final_diff = current_diff + i - j
            prob = pi * pj
            if final_diff > 0:
                p_home += prob
            elif final_diff == 0:
                p_draw += prob
            else:
                p_away += prob

    total = p_home + p_draw + p_away
    if total <= 0.0:
        return {sel: 1.0 / 3.0 for sel in RESULT_SELECTIONS}
    raw = {"HOME": p_home / total, "DRAW": p_draw / total, "AWAY": p_away / total}
    return {sel: min(1.0 - _EPS, max(_EPS, value)) for sel, value in raw.items()}


def demo_timeline(
    prior: dict[str, float] | None = None,
    *,
    match_length: int = 90,
    step: int = 10,
) -> dict[str, Any]:
    """A scripted match the model reacts to, sampled every ``step`` minutes.

    HOME leads at 20', AWAY equalizes at 60', HOME wins it at 82'. Judges can see
    the probability distribution move at each goal and converge by full time.
    """

    prior = prior or {"HOME": 0.50, "DRAW": 0.27, "AWAY": 0.23}
    goals = [(20, "HOME"), (60, "AWAY"), (82, "HOME")]
    timeline = []
    for minute in range(0, match_length + 1, step):
        home_score = sum(1 for at, sel in goals if sel == "HOME" and at <= minute)
        away_score = sum(1 for at, sel in goals if sel == "AWAY" and at <= minute)
        probs = match_result_probabilities(
            prior,
            minute=minute,
            home_score=home_score,
            away_score=away_score,
            match_length=match_length,
        )
        timeline.append(
            {
                "minute": minute,
                "home_score": home_score,
                "away_score": away_score,
                "probabilities": {sel: round(value, 4) for sel, value in probs.items()},
            }
        )
    return {
        "schema": "proofguard.in-play-model-timeline.v1",
        "model": "deterministic in-play Poisson (prior + score + time)",
        "prior": _normalized_prior(prior),
        "goals": [{"minute": at, "selection": sel} for at, sel in goals],
        "timeline": timeline,
    }
