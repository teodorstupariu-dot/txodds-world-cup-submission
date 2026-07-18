from __future__ import annotations

from datetime import datetime, timezone

from finalitygate import (
    FinalityGateResolver,
    OutcomeMarket,
    ResolutionEvidence,
    commitment_anchor,
    explain_decision,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _decision(**ev) -> dict:
    market = OutcomeMarket(market_id="m1", fixture_id="f1", market_type="MATCH_RESULT", selections=("HOME", "DRAW", "AWAY"))
    base = dict(
        fixture_id="f1", fixture_status="FINAL", home_score=2, away_score=1, declared_result="HOME",
        observed_at=NOW, proof_status="VALID", root_status="MATCH", proof_reference="p",
        expected_root="cc" * 32, observed_root="cc" * 32,
    )
    base.update(ev)
    return FinalityGateResolver().resolve(market, ResolutionEvidence(**base)).to_dict()


def test_explain_resolved_case() -> None:
    ex = explain_decision(_decision())
    assert ex["state"] == "RESOLVE"
    assert ex["settled"] is True and ex["fail_closed"] is False
    assert "fixture is final" in ex["checks_passed"]
    assert ex["remediation"][0]["reason"] == "finality_result_proof_and_root_agree"


def test_explain_dispute_gives_remediation() -> None:
    ex = explain_decision(_decision(declared_result="AWAY"))  # conflicts with 2-1 score
    assert ex["state"] == "DISPUTE"
    assert ex["fail_closed"] is True
    reasons = [r["reason"] for r in ex["remediation"]]
    assert "declared_result_conflicts_with_score" in reasons
    assert all(r["action"] for r in ex["remediation"])


def test_explain_wait_for_proof_lists_missing_evidence() -> None:
    ex = explain_decision(_decision(proof_status="MISSING"))
    assert ex["state"] == "WAIT_FOR_PROOF"
    assert "declared result matches score" in ex["checks_passed"]


def test_onchain_anchor_is_honest_and_deterministic() -> None:
    root = "ab" * 32
    a = commitment_anchor(root)
    b = commitment_anchor(root)
    assert a["root_is_canonical_32_bytes"] is True
    assert a["onchain_call_executed"] is False
    assert a["network"] is None
    assert a["pda_seed_inputs"] == ["daily_scores_roots", root]
    assert len(a["illustrative_anchor_digest"]) == 64
    assert a == b  # deterministic
    assert "NOT a real Solana" in a["note"]


def test_onchain_anchor_rejects_non_canonical_root() -> None:
    a = commitment_anchor("not-a-root")
    assert a["root_is_canonical_32_bytes"] is False
    assert a["illustrative_anchor_digest"] is None
