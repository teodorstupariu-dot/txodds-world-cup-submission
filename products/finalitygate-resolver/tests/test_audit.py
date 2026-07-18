"""Adversarial regression tests for FinalityGate (audit Phase 2 A).

A1 - canonical-root enforcement for RESOLVE (fail-closed).
A2 - receipt-envelope integrity in verify_receipt.
A3 - fail-closed matrix: no invalid/incomplete case may RESOLVE.
A4 - web/API hardening (malformed body, non-finite numbers, health 503, ...).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from finalitygate.core import (
    FinalityGateResolver,
    OutcomeMarket,
    ResolutionEvidence,
    verify_receipt,
)
from finalitygate.web.app import build_health, create_app

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
MARKET = OutcomeMarket("market-1", "fixture-1", "MATCH_RESULT", ("HOME", "DRAW", "AWAY"))
CANON = "cc" * 32
RESOLVER = FinalityGateResolver()


def _evidence(**overrides) -> ResolutionEvidence:
    base = dict(
        fixture_id="fixture-1",
        fixture_status="FINAL",
        home_score=2,
        away_score=1,
        declared_result="HOME",
        observed_at=NOW,
        proof_status="VALID",
        root_status="MATCH",
        proof_reference="proof-1",
        expected_root=CANON,
        observed_root=CANON,
        source_fingerprint="txline-schema-v1",
    )
    base.update(overrides)
    return ResolutionEvidence(**base)


# ---------------------------------------------------------------------------
# A1 - canonical roots
# ---------------------------------------------------------------------------
def test_a1_noncanonical_equal_roots_do_not_resolve() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root="x", observed_root="x"))
    assert decision.state == "DISPUTE"
    assert decision.state != "RESOLVE"


def test_a1_wrong_length_root_does_not_resolve() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root="cc" * 31, observed_root="cc" * 31))
    assert decision.state == "DISPUTE"


def test_a1_non_hex_root_does_not_resolve() -> None:
    bad = "z" * 64
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=bad, observed_root=bad))
    assert decision.state == "DISPUTE"


def test_a1_one_valid_one_invalid_root_does_not_resolve() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=CANON, observed_root="x"))
    assert decision.state == "DISPUTE"


def test_a1_two_valid_case_insensitive_roots_resolve() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=CANON, observed_root=CANON.upper()))
    assert decision.state == "RESOLVE"
    assert decision.resolved_selection == "HOME"


def test_a1_two_valid_different_roots_dispute() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root="aa" * 32, observed_root="bb" * 32))
    assert decision.state == "DISPUTE"


def test_a1_is_canonical_root_strict_boundaries() -> None:
    is_canon = FinalityGateResolver._is_canonical_root
    assert is_canon(None) is False
    assert is_canon("") is False
    assert is_canon("cc" * 32) is True
    assert is_canon(("cc" * 32).upper()) is True
    assert is_canon(" " + "cc" * 32) is False
    assert is_canon("cc" * 32 + " ") is False
    assert is_canon("\n" + "cc" * 32) is False
    assert is_canon("0x" + "cc" * 32) is False
    assert is_canon("z" * 64) is False
    assert is_canon("cc" * 31) is False
    assert is_canon("cc" * 33) is False


@pytest.mark.parametrize(
    "padded",
    [
        " " + CANON,
        CANON + " ",
        "\n" + CANON,
        "0x" + CANON,
    ],
)
def test_a1_whitespace_or_prefixed_equal_roots_dispute(padded: str) -> None:
    # Equal but non-canonical roots (leading/trailing whitespace, newline, 0x
    # prefix) must never RESOLVE: they are invalid material, so fail closed.
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=padded, observed_root=padded))
    assert decision.state == "DISPUTE"
    assert decision.state != "RESOLVE"


def test_a1_uppercase_expected_vs_lowercase_observed_resolves() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=CANON.upper(), observed_root=CANON))
    assert decision.state == "RESOLVE"
    assert decision.resolved_selection == "HOME"


def test_a1_absent_roots_wait_for_proof_not_dispute() -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(expected_root=None, observed_root=None))
    assert decision.state == "WAIT_FOR_PROOF"


def test_a1_onchain_boundary_unchanged() -> None:
    # No fabricated Solana validation; the resolver never asserts on-chain views.
    with TestClient(create_app()) as client:
        status = client.get("/api/status").json()
        assert status["onchain_view_executed"] is False


# ---------------------------------------------------------------------------
# A2 - receipt envelope integrity
# ---------------------------------------------------------------------------
def _resolved_envelope() -> dict:
    return RESOLVER.resolve(MARKET, _evidence()).to_dict()


def test_a2_original_receipt_passes() -> None:
    assert verify_receipt(_resolved_envelope())["status"] == "PASS"


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "DISPUTE"),
        ("market_id", "tampered-market"),
        ("fixture_id", "tampered-fixture"),
        ("resolved_selection", "AWAY"),
        ("reasons", ["fabricated_reason"]),
        ("checks", {"fabricated": True}),
    ],
)
def test_a2_outer_envelope_tamper_fails(field: str, value) -> None:
    envelope = _resolved_envelope()
    envelope[field] = value  # receipt_payload untouched
    result = verify_receipt(envelope)
    assert result["status"] == "FAIL"
    assert any(field in err for err in result["errors"])


def test_a2_hash_only_payload_still_verifies() -> None:
    envelope = _resolved_envelope()
    minimal = {"receipt_payload": envelope["receipt_payload"], "receipt_sha256": envelope["receipt_sha256"]}
    assert verify_receipt(minimal)["status"] == "PASS"


def test_a2_receipt_payload_tamper_fails() -> None:
    envelope = _resolved_envelope()
    envelope["receipt_payload"]["state"] = "RESOLVE_FAKE"
    assert verify_receipt(envelope)["status"] == "FAIL"


# ---------------------------------------------------------------------------
# A3 - fail-closed matrix
# ---------------------------------------------------------------------------
FAIL_CLOSED_MATRIX = [
    ("fixture_identity_mismatch", {"fixture_id": "other-fixture"}, "DISPUTE"),
    ("unknown_final_status", {"fixture_status": "MYSTERY"}, "PENDING_FINALITY"),
    ("incomplete_score", {"home_score": None}, "PENDING_FINALITY"),
    ("declared_missing", {"declared_result": None}, "PENDING_FINALITY"),
    ("declared_not_allowed", {"declared_result": "ZED"}, "DISPUTE"),
    ("score_result_contradiction", {"declared_result": "AWAY"}, "DISPUTE"),
    ("proof_invalid", {"proof_status": "INVALID"}, "DISPUTE"),
    ("proof_missing", {"proof_status": "MISSING", "proof_reference": None}, "WAIT_FOR_PROOF"),
    ("proof_valid_no_reference", {"proof_reference": None}, "WAIT_FOR_PROOF"),
    ("root_missing", {"root_status": "MISSING"}, "WAIT_FOR_PROOF"),
    ("root_unverified", {"root_status": "UNVERIFIED"}, "WAIT_FOR_PROOF"),
    ("root_mismatch", {"root_status": "MISMATCH"}, "DISPUTE"),
    ("root_match_no_values", {"expected_root": None, "observed_root": None}, "WAIT_FOR_PROOF"),
    ("roots_invalid", {"expected_root": "x", "observed_root": "x"}, "DISPUTE"),
    ("roots_different", {"expected_root": "aa" * 32, "observed_root": "bb" * 32}, "DISPUTE"),
    ("all_valid", {}, "RESOLVE"),
]


@pytest.mark.parametrize("name,overrides,expected", FAIL_CLOSED_MATRIX, ids=[r[0] for r in FAIL_CLOSED_MATRIX])
def test_a3_fail_closed_matrix(name: str, overrides: dict, expected: str) -> None:
    decision = RESOLVER.resolve(MARKET, _evidence(**overrides))
    assert decision.state == expected
    if expected != "RESOLVE":
        assert decision.state != "RESOLVE"
        assert decision.resolved_selection is None


def test_a3_only_all_valid_resolves() -> None:
    resolves = [
        name
        for name, overrides, _ in FAIL_CLOSED_MATRIX
        if RESOLVER.resolve(MARKET, _evidence(**overrides)).state == "RESOLVE"
    ]
    assert resolves == ["all_valid"]


# ---------------------------------------------------------------------------
# A4 - web/API hardening
# ---------------------------------------------------------------------------
WEB_REQUEST = {
    "market": {
        "market_id": "market-1",
        "fixture_id": "fixture-1",
        "market_type": "MATCH_RESULT",
        "selections": ["HOME", "DRAW", "AWAY"],
        "policy_version": "finalitygate-v1",
    },
    "evidence": {
        "fixture_id": "fixture-1",
        "fixture_status": "FINAL",
        "home_score": 2,
        "away_score": 1,
        "declared_result": "HOME",
        "observed_at": "2026-07-10T12:00:00Z",
        "proof_status": "VALID",
        "root_status": "MATCH",
        "proof_reference": "proof-1",
        "expected_root": CANON,
        "observed_root": CANON,
        "source_fingerprint": "txline-schema-v1",
    },
}


def test_a4_missing_body_rejected() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/resolve").status_code == 422


def test_a4_malformed_json_rejected() -> None:
    with TestClient(create_app()) as client:
        resp = client.post(
            "/api/resolve",
            content="{ this is not json ",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422


def test_a4_invalid_timestamp_rejected() -> None:
    with TestClient(create_app()) as client:
        payload = copy.deepcopy(WEB_REQUEST)
        payload["evidence"]["observed_at"] = "not-a-timestamp"
        assert client.post("/api/resolve", json=payload).status_code == 422


def test_a4_nan_and_infinity_rejected() -> None:
    with TestClient(create_app()) as client:
        for token in ("NaN", "Infinity", "-Infinity"):
            body = (
                '{"market":{"market_id":"m","fixture_id":"f","market_type":"MATCH_RESULT",'
                '"selections":["HOME","DRAW","AWAY"],"policy_version":"finalitygate-v1"},'
                '"evidence":{"fixture_id":"f","fixture_status":"FINAL","home_score":' + token + ','
                '"away_score":1,"declared_result":"HOME","observed_at":"2026-07-10T12:00:00Z",'
                '"proof_status":"VALID","root_status":"MATCH","proof_reference":"p"}}'
            )
            resp = client.post("/api/resolve", content=body, headers={"content-type": "application/json"})
            assert resp.status_code == 422, token


def test_a4_web_resolve_noncanonical_roots_dispute_not_resolve() -> None:
    with TestClient(create_app()) as client:
        payload = copy.deepcopy(WEB_REQUEST)
        payload["evidence"]["expected_root"] = "x"
        payload["evidence"]["observed_root"] = "x"
        resp = client.post("/api/resolve", json=payload)
        assert resp.status_code == 200
        assert resp.json()["state"] == "DISPUTE"


def test_a4_web_verify_receipt_envelope_tamper_fails() -> None:
    with TestClient(create_app()) as client:
        decision = client.post("/api/resolve", json=WEB_REQUEST).json()
        assert client.post("/api/verify-receipt", json=decision).json()["status"] == "PASS"
        decision["state"] = "DISPUTE"
        assert client.post("/api/verify-receipt", json=decision).json()["status"] == "FAIL"


def test_a4_health_503_when_internal_summary_invalid() -> None:
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        # Corrupt the in-memory summary: health must fail closed with 503.
        app.state.summary = {"status": "FAIL", "state_counts": {}, "cases": [], "case_count": 0}
        resp = client.get("/api/health")
        assert resp.status_code == 503
        assert resp.json()["status"] == "FAIL"


def test_o1_web_startup_creates_no_temp_files() -> None:
    # The web app must build its summary purely in memory (no TemporaryDirectory).
    import tempfile
    from pathlib import Path

    temp_root = Path(tempfile.gettempdir())

    def fg_dirs() -> set[str]:
        return {p.name for p in temp_root.glob("fg-*")}

    before = fg_dirs()
    with TestClient(create_app()) as client:
        assert client.get("/api/demo").json()["status"] == "PASS"
    assert fg_dirs() <= before  # no new fg-* temp directories created


def test_o1_pure_summary_matches_persisted_summary(tmp_path) -> None:
    from finalitygate.demo import build_demo_summary, run_demo

    persisted = run_demo(tmp_path / "demo")
    persisted.pop("manifest", None)
    assert build_demo_summary() == persisted


def test_a4_build_health_requires_all_states_and_receipts() -> None:
    # Missing a state -> FAIL even if demo status is PASS.
    partial = {
        "status": "PASS",
        "state_counts": {"OPEN": 1, "PENDING_FINALITY": 1, "WAIT_FOR_PROOF": 1, "DISPUTE": 1},
        "cases": [{"receipt_verification": {"status": "PASS"}}],
        "case_count": 1,
    }
    assert build_health(partial)["status"] == "FAIL"
