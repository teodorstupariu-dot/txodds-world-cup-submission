"""Tests for the naive-resolver settlement-impact counterfactual."""

from __future__ import annotations

from finalitygate import settlement_impact
from finalitygate.demo import build_demo_summary


def test_settlement_impact_over_demo_cases() -> None:
    decisions = [case["decision"] for case in build_demo_summary()["cases"]]
    impact = settlement_impact(decisions)

    # The demo has final markets that are WAIT_FOR_PROOF / DISPUTE — a naive
    # resolver would settle all of them on the declared result; FinalityGate
    # refuses each. So at least one unsafe settlement is prevented.
    assert impact["markets_considered"] == len(decisions)
    assert impact["unsafe_settlements_prevented"] >= 1
    # Every prevented settlement is also a naive settlement, and never exceeds it.
    assert impact["unsafe_settlements_prevented"] <= impact["naive_settlements"]
    # Reasons are recorded for the prevented settlements.
    assert sum(impact["unsafe_settlement_reasons"].values()) >= impact["unsafe_settlements_prevented"]


def test_settlement_impact_ignores_non_final_markets() -> None:
    # A market that is not final is never settled by the naive resolver, so it
    # cannot count as a prevented settlement.
    decision = {
        "state": "OPEN",
        "checks": {"fixture_final": False, "declared_result_present": True, "declared_result_allowed": True},
        "reasons": ["fixture_not_final"],
    }
    impact = settlement_impact([decision])
    assert impact["naive_settlements"] == 0
    assert impact["unsafe_settlements_prevented"] == 0


def test_settlement_impact_counts_a_resolve_as_safe() -> None:
    decision = {
        "state": "RESOLVE",
        "checks": {"fixture_final": True, "declared_result_present": True, "declared_result_allowed": True},
        "reasons": ["finality_result_proof_and_root_agree"],
    }
    impact = settlement_impact([decision])
    assert impact["naive_settlements"] == 1
    assert impact["unsafe_settlements_prevented"] == 0
