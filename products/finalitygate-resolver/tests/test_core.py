from __future__ import annotations

from datetime import datetime, timezone

from finalitygate import FinalityGateResolver, OutcomeMarket, ResolutionEvidence, verify_receipt

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
ROOT = "aa" * 32


def market() -> OutcomeMarket:
    return OutcomeMarket(
        market_id="market-1",
        fixture_id="fixture-1",
        market_type="MATCH_RESULT",
        selections=("HOME", "DRAW", "AWAY"),
    )


def evidence(
    *,
    status: str = "FINAL",
    home: int | None = 2,
    away: int | None = 1,
    declared: str | None = "HOME",
    proof: str = "VALID",
    root: str = "MATCH",
    fixture_id: str = "fixture-1",
    proof_reference: str | None = "proof-batch-1",
    expected_root: str | None = ROOT,
    observed_root: str | None = ROOT,
) -> ResolutionEvidence:
    return ResolutionEvidence(
        fixture_id=fixture_id,
        fixture_status=status,
        home_score=home,
        away_score=away,
        declared_result=declared,
        observed_at=NOW,
        proof_status=proof,  # type: ignore[arg-type]
        root_status=root,  # type: ignore[arg-type]
        proof_reference=proof_reference,
        expected_root=expected_root,
        observed_root=observed_root,
    )


def test_scheduled_market_remains_open() -> None:
    decision = FinalityGateResolver().resolve(
        market(),
        evidence(status="SCHEDULED", home=None, away=None, declared=None, proof="MISSING", root="MISSING", proof_reference=None, expected_root=None, observed_root=None),
    )
    assert decision.state == "OPEN"
    assert decision.resolved_selection is None


def test_live_market_is_pending_finality() -> None:
    decision = FinalityGateResolver().resolve(
        market(),
        evidence(status="LIVE", proof="UNVERIFIED", root="UNVERIFIED", proof_reference=None, expected_root=None, observed_root=None),
    )
    assert decision.state == "PENDING_FINALITY"


def test_final_result_waits_for_missing_proof() -> None:
    decision = FinalityGateResolver().resolve(
        market(),
        evidence(proof="MISSING", root="MISSING", proof_reference=None, expected_root=None, observed_root=None),
    )
    assert decision.state == "WAIT_FOR_PROOF"
    assert "proof_not_ready" in decision.reasons


def test_valid_status_without_proof_reference_waits() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence(proof_reference=None))
    assert decision.state == "WAIT_FOR_PROOF"
    assert "proof_reference_missing" in decision.reasons


def test_matching_root_status_without_values_waits() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence(expected_root=None, observed_root=None))
    assert decision.state == "WAIT_FOR_PROOF"
    assert "root_values_missing" in decision.reasons


def test_result_conflict_opens_dispute() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence(declared="AWAY"))
    assert decision.state == "DISPUTE"
    assert "declared_result_conflicts_with_score" in decision.reasons


def test_fixture_identity_conflict_opens_dispute() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence(fixture_id="other-fixture"))
    assert decision.state == "DISPUTE"
    assert "fixture_identity_conflict" in decision.reasons


def test_invalid_proof_opens_dispute() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence(proof="INVALID"))
    assert decision.state == "DISPUTE"
    assert "proof_invalid" in decision.reasons


def test_root_value_conflict_opens_dispute_even_when_status_says_match() -> None:
    decision = FinalityGateResolver().resolve(
        market(),
        evidence(expected_root="aa" * 32, observed_root="bb" * 32),
    )
    assert decision.state == "DISPUTE"
    assert "declared_root_values_conflict" in decision.reasons


def test_complete_consistent_evidence_resolves() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence())
    assert decision.state == "RESOLVE"
    assert decision.resolved_selection == "HOME"
    assert decision.checks["fail_closed"] is True


def test_resolution_receipt_verifies_and_tampering_fails() -> None:
    decision = FinalityGateResolver().resolve(market(), evidence()).to_dict()
    assert verify_receipt(decision)["status"] == "PASS"

    decision["receipt_payload"]["resolved_selection"] = "AWAY"
    assert verify_receipt(decision)["status"] == "FAIL"


def test_receipt_is_deterministic() -> None:
    resolver = FinalityGateResolver()
    first = resolver.resolve(market(), evidence()).receipt_sha256
    second = resolver.resolve(market(), evidence()).receipt_sha256
    assert first == second


def test_market_rejects_duplicate_selections() -> None:
    try:
        OutcomeMarket(
            market_id="market-1",
            fixture_id="fixture-1",
            market_type="MATCH_RESULT",
            selections=("HOME", "HOME"),
        )
    except ValueError as exc:
        assert "unique" in str(exc)
    else:
        raise AssertionError("duplicate selections must fail")
