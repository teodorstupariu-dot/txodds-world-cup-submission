"""Human-readable provenance for a FinalityGate resolution decision.

The resolver emits a machine verdict (state + reasons + a rich ``checks`` map).
This module turns that into an auditor-facing explanation: which evidence checks
passed, which failed, a dispute taxonomy, and concrete remediation / next
actions. It is a pure, additive read over an existing decision — it never
changes the fail-closed state machine.
"""

from __future__ import annotations

from typing import Any

# state -> (category, one-line summary)
_STATE_SUMMARY: dict[str, tuple[str, str]] = {
    "OPEN": ("not_yet_resolvable", "Market is live; the fixture has not started or finished."),
    "PENDING_FINALITY": ("awaiting_finality", "A result may exist but the fixture is not final enough to settle."),
    "WAIT_FOR_PROOF": ("awaiting_proof", "The result is final but proof/on-chain-root evidence is incomplete."),
    "DISPUTE": ("evidence_conflict", "Evidence conflicts; settlement is refused fail-closed until resolved."),
    "RESOLVE": ("settled", "All required evidence agrees; the market is resolved."),
}

# reason -> concrete remediation / next action for an operator or oracle.
_REMEDIATION: dict[str, str] = {
    "fixture_not_final": "Wait for the fixture to reach a final status (FT/AET/PEN).",
    "unrecognized_or_nonfinal_fixture_status": "Normalize the fixture status to a recognized final/non-final value.",
    "final_result_incomplete": "Supply the complete final score and the declared result.",
    "declared_result_not_allowed_by_market": "Declared result must be one of the market's selections; fix the feed mapping.",
    "declared_result_conflicts_with_score": "Declared result disagrees with the score; reconcile the scores/result source.",
    "fixture_identity_conflict": "Market and evidence fixture IDs differ; route the correct fixture's evidence.",
    "proof_invalid": "Proof material is INVALID; obtain a valid proof before settlement.",
    "onchain_root_mismatch": "Observed on-chain root does not match; re-derive or re-anchor the root.",
    "declared_root_values_conflict": "Declared root values are non-canonical or disagree; provide canonical 32-byte roots.",
    "proof_not_ready": "Proof is MISSING/UNVERIFIED; wait for the proof to be produced and verified.",
    "proof_reference_missing": "Proof is VALID but its reference is missing; attach the concrete proof reference.",
    "onchain_root_not_confirmed": "On-chain root is not yet confirmed; wait for anchoring/confirmation.",
    "root_values_missing": "Root status is MATCH but the expected/observed root values are absent; supply them.",
    "resolution_requirements_incomplete": "One or more required evidence fields are still missing; complete them.",
    "finality_result_proof_and_root_agree": "No action needed — settlement conditions are fully satisfied.",
}

# checks map key -> readable label
_CHECK_LABELS: dict[str, str] = {
    "fixture_identity_match": "fixture identity matches",
    "fixture_final": "fixture is final",
    "score_complete": "final score present",
    "declared_result_present": "declared result present",
    "declared_result_allowed": "declared result allowed by market",
    "declared_matches_score": "declared result matches score",
    "proof_reference_present": "proof reference present",
    "root_values_present": "on-chain root values present",
}


def explain_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Produce an auditor-facing explanation of a resolution decision."""

    payload = decision.get("receipt_payload", decision)
    state = payload.get("state", decision.get("state"))
    reasons = payload.get("reasons", decision.get("reasons", []))
    checks = payload.get("checks", {}) if isinstance(payload.get("checks"), dict) else {}

    category, summary = _STATE_SUMMARY.get(state, ("unknown", "Unrecognized resolution state."))

    passed: list[str] = []
    failed: list[str] = []
    for key, label in _CHECK_LABELS.items():
        if key not in checks:
            continue
        (passed if checks.get(key) else failed).append(label)

    remediation = [
        {"reason": reason, "action": _REMEDIATION.get(reason, "Review this reason and supply the missing/consistent evidence.")}
        for reason in reasons
    ]

    return {
        "schema": "finalitygate.resolution-explanation.v1",
        "state": state,
        "category": category,
        "summary": summary,
        "settled": state == "RESOLVE",
        "fail_closed": state != "RESOLVE",
        "checks_passed": passed,
        "checks_failed": failed,
        "reasons": list(reasons),
        "remediation": remediation,
        "proof_status": checks.get("proof_status"),
        "root_status": checks.get("root_status"),
    }
