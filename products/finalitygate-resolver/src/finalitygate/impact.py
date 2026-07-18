"""Quantify FinalityGate's value against a naive-resolver baseline.

The naive baseline settles a market on its *declared result* as soon as the
fixture is final — the way an unguarded settlement bot would. FinalityGate
additionally requires proof, on-chain root, and score evidence to agree. Every
market that is final with an allowed declared result, yet does NOT reach
``RESOLVE``, is an unsafe settlement the naive resolver would have made (on
missing proof, a mismatched root, or a score that contradicts the declared
result) and FinalityGate refused.

This is a deterministic counterfactual over real resolution decisions; no
monetary figure is asserted.
"""

from __future__ import annotations

from typing import Any


def settlement_impact(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare FinalityGate's fail-closed decisions to a naive resolver."""

    naive_settlements = 0
    unsafe_prevented = 0
    prevented_reasons: dict[str, int] = {}
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        checks = decision.get("checks", {})
        naive_would_settle = bool(
            checks.get("fixture_final")
            and checks.get("declared_result_present")
            and checks.get("declared_result_allowed")
        )
        if not naive_would_settle:
            continue
        naive_settlements += 1
        if decision.get("state") != "RESOLVE":
            unsafe_prevented += 1
            for reason in decision.get("reasons", []) or ["unspecified"]:
                prevented_reasons[reason] = prevented_reasons.get(reason, 0) + 1

    total = len(decisions)
    return {
        "baseline": "naive resolver (settles on the declared result as soon as the fixture is final, ignoring proof, on-chain root, and score agreement)",
        "markets_considered": total,
        "naive_settlements": naive_settlements,
        "unsafe_settlements_prevented": unsafe_prevented,
        "unsafe_settlement_reasons": prevented_reasons,
        "note": "deterministic counterfactual over real resolution decisions; no monetary value is claimed",
    }
