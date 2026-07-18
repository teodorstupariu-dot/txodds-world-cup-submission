from __future__ import annotations

from datetime import datetime, timezone

from finalitygate import (
    FinalityGateResolver,
    MerkleTree,
    OutcomeMarket,
    ResolutionEvidence,
    build_commitment,
    verify_proof,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _resolved_decision() -> dict:
    market = OutcomeMarket(
        market_id="market-1",
        fixture_id="fixture-1",
        market_type="MATCH_RESULT",
        selections=("HOME", "DRAW", "AWAY"),
    )
    evidence = ResolutionEvidence(
        fixture_id="fixture-1",
        fixture_status="FINAL",
        home_score=2,
        away_score=1,
        declared_result="HOME",
        observed_at=NOW,
        proof_status="VALID",
        root_status="MATCH",
        proof_reference="proof-1",
        expected_root="cc" * 32,
        observed_root="cc" * 32,
        source_fingerprint="txline-schema-v1",
    )
    return FinalityGateResolver().resolve(market, evidence).to_dict()


def test_root_is_a_canonical_32_byte_value() -> None:
    commit = build_commitment(_resolved_decision())
    assert commit["root_bytes"] == 32
    assert len(commit["root"]) == 64
    assert all(ch in "0123456789abcdef" for ch in commit["root"])


def test_commitment_is_deterministic() -> None:
    a = build_commitment(_resolved_decision())
    b = build_commitment(_resolved_decision())
    assert a["root"] == b["root"]


def test_every_fact_has_a_verifying_inclusion_proof() -> None:
    commit = build_commitment(_resolved_decision())
    root = commit["root"]
    for leaf in commit["leaves"]:
        proof = commit["proofs"][leaf["field"]]
        assert verify_proof(leaf["leaf_hash"], proof, root) is True


def test_tampered_proof_fails() -> None:
    commit = build_commitment(_resolved_decision())
    root = commit["root"]
    leaf = commit["leaves"][0]
    proof = list(commit["proofs"][leaf["field"]])
    if proof:
        bad = dict(proof[0])
        bad["sibling"] = "00" * 32
        proof[0] = bad
        assert verify_proof(leaf["leaf_hash"], proof, root) is False


def test_changing_a_fact_changes_the_root() -> None:
    decision = _resolved_decision()
    root_a = build_commitment(decision)["root"]
    # Flip the declared result: the committed root must change.
    decision["receipt_payload"]["evidence"]["declared_result"] = "AWAY"
    root_b = build_commitment(decision)["root"]
    assert root_a != root_b


def test_merkle_root_matches_manual_single_leaf() -> None:
    tree = MerkleTree([("only", "fact")])
    assert len(tree.root) == 64
    assert verify_proof(tree.leaf_digests[0].hex(), tree.proof(0), tree.root) is True
