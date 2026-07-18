from __future__ import annotations

from proofguard_agent.model import (
    demo_timeline,
    match_result_probabilities,
    prematch_goal_rates,
)

PRIOR = {"HOME": 0.50, "DRAW": 0.27, "AWAY": 0.23}


def test_probabilities_are_a_valid_distribution() -> None:
    probs = match_result_probabilities(PRIOR, minute=0, home_score=0, away_score=0)
    assert set(probs) == {"HOME", "DRAW", "AWAY"}
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert all(0.0 < value < 1.0 for value in probs.values())


def test_prematch_ordering_follows_prior() -> None:
    probs = match_result_probabilities(PRIOR, minute=0, home_score=0, away_score=0)
    # A home-favored prior yields a home-favored fresh match.
    assert probs["HOME"] > probs["AWAY"]


def test_a_goal_shifts_probability_toward_the_scorer() -> None:
    before = match_result_probabilities(PRIOR, minute=25, home_score=0, away_score=0)
    after = match_result_probabilities(PRIOR, minute=25, home_score=1, away_score=0)
    assert after["HOME"] > before["HOME"]
    assert after["AWAY"] < before["AWAY"]


def test_time_running_out_converges_on_the_leader() -> None:
    early = match_result_probabilities(PRIOR, minute=50, home_score=1, away_score=0)
    late = match_result_probabilities(PRIOR, minute=89, home_score=1, away_score=0)
    assert late["HOME"] > early["HOME"]
    assert late["HOME"] > 0.9


def test_full_time_result_is_certain() -> None:
    probs = match_result_probabilities(PRIOR, minute=90, home_score=2, away_score=1)
    assert probs["HOME"] > 0.99


def test_prematch_goal_rates_reflect_supremacy_and_conserve_total() -> None:
    lam_home, lam_away = prematch_goal_rates(PRIOR, base_total_goals=2.7)
    assert lam_home > lam_away
    assert abs((lam_home + lam_away) - 2.7) < 1e-9


def test_model_is_deterministic() -> None:
    a = match_result_probabilities(PRIOR, minute=33, home_score=1, away_score=1)
    b = match_result_probabilities(PRIOR, minute=33, home_score=1, away_score=1)
    assert a == b


def test_demo_timeline_reacts_to_scripted_goals() -> None:
    timeline = demo_timeline()["timeline"]
    assert timeline[0]["minute"] == 0
    assert timeline[-1]["minute"] == 90
    # Final sampled state is HOME 2-1 (goals at 20', 60', 82') and near-certain.
    assert timeline[-1]["home_score"] == 2
    assert timeline[-1]["away_score"] == 1
    assert timeline[-1]["probabilities"]["HOME"] > 0.9
    assert all(abs(sum(row["probabilities"].values()) - 1.0) < 1e-3 for row in timeline)
