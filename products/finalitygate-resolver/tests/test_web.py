from __future__ import annotations

import copy
import json
import os

from fastapi.testclient import TestClient

from finalitygate.web.app import REQUIRED_STATES, create_app

ROOT_HEX = "cc" * 32

BASE_REQUEST = {
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
        "expected_root": ROOT_HEX,
        "observed_root": ROOT_HEX,
        "source_fingerprint": "txline-schema-v1",
    },
}


def _request(**evidence_overrides) -> dict:
    payload = copy.deepcopy(BASE_REQUEST)
    payload["evidence"].update(evidence_overrides)
    return payload


def _resolve_state(client: TestClient, **evidence_overrides) -> dict:
    response = client.post("/api/resolve", json=_request(**evidence_overrides))
    assert response.status_code == 200, response.text
    return response.json()


# 1. dashboard + all endpoints
def test_dashboard_and_all_endpoints_respond() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        assert "FinalityGate" in client.get("/").text
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/status").status_code == 200
        assert client.get("/api/demo").status_code == 200
        assert client.get("/api/docs").status_code == 200
        openapi = client.get("/api/openapi.json")
        assert openapi.status_code == 200
        paths = set(openapi.json()["paths"])
        assert {"/api/health", "/api/status", "/api/demo", "/api/resolve", "/api/verify-receipt"} <= paths
        assert client.post("/api/resolve", json=BASE_REQUEST).status_code == 200
        assert client.post("/api/verify-receipt", json={}).status_code == 200


def test_explorer_page_and_commitments_demo() -> None:
    with TestClient(create_app()) as client:
        page = client.get("/explorer")
        assert page.status_code == 200
        assert "Merkle Proof Explorer" in page.text
        assert "fetch('api/commitments/demo'" in page.text  # relative, mount-safe
        assert 'href="/api/' not in page.text and "href='/api/" not in page.text
        data = client.get("/api/commitments/demo").json()
        assert data["count"] >= 5
        first = data["commitments"][0]["commitment"]
        assert len(first["root"]) == 64 and first["leaves"] and first["proofs"]


def test_impact_endpoint_and_dashboard_banner() -> None:
    with TestClient(create_app()) as client:
        data = client.get("/api/impact").json()
        assert data["unsafe_settlements_prevented"] >= 1
        assert data["unsafe_settlements_prevented"] <= data["naive_settlements"]
        # The dashboard surfaces the impact headline (no leftover template tokens).
        page = client.get("/").text
        assert "Settlement impact" in page
        assert "unsafe settlements prevented" in page
        assert "__IMP_" not in page


def test_explain_endpoint_returns_remediation() -> None:
    with TestClient(create_app()) as client:
        ex = client.post("/api/explain", json=_request(declared_result="AWAY")).json()
        assert ex["state"] == "DISPUTE"
        assert any(r["reason"] == "declared_result_conflicts_with_score" for r in ex["remediation"])


def test_onchain_anchor_endpoint_is_non_executing() -> None:
    with TestClient(create_app()) as client:
        a = client.get("/api/onchain-anchor").json()
        assert a["onchain_call_executed"] is False
        assert a["root_is_canonical_32_bytes"] is True
        assert len(a["root"]) == 64


def test_ledger_endpoint_over_demo_cases_verifies() -> None:
    with TestClient(create_app()) as client:
        body = client.get("/api/ledger").json()
        assert body["ledger"]["count"] >= 5
        assert len(body["ledger"]["batch_root"]) == 64
        assert body["verification"]["status"] == "PASS"


def test_batch_resolve_builds_verifiable_ledger() -> None:
    with TestClient(create_app()) as client:
        second = copy.deepcopy(BASE_REQUEST)
        second["market"]["market_id"] = "market-2"
        payload = {"resolutions": [BASE_REQUEST, second]}
        resp = client.post("/api/resolve/batch", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ledger"]["count"] == 2
        assert body["verification"]["status"] == "PASS"
        # Empty batch is rejected.
        assert client.post("/api/resolve/batch", json={"resolutions": []}).status_code == 422


def test_commitment_endpoint_returns_verifiable_root() -> None:
    with TestClient(create_app()) as client:
        response = client.post("/api/commitment", json=BASE_REQUEST)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["decision"]["state"] == "RESOLVE"
        commit = body["commitment"]
        assert commit["root_bytes"] == 32 and len(commit["root"]) == 64
        assert commit["leaf_count"] == len(commit["leaves"])
        # The server self-checks that the declared result is provably committed.
        assert body["inclusion_proof_self_check"]["field"] == "declared_result"
        assert body["inclusion_proof_self_check"]["verified"] is True
        # The Merkle root is a real commitment: a different result changes it.
        other = client.post("/api/commitment", json=_request(declared_result="DRAW", home_score=1, away_score=1)).json()
        assert other["commitment"]["root"] != commit["root"]


# 2. health PASS and all five states present
def test_health_pass_covers_all_five_states() -> None:
    with TestClient(create_app()) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "PASS"
        assert set(REQUIRED_STATES).issubset(body["state_counts"])
        assert body["checks"]["all_states_present"] is True
        assert body["checks"]["demo_receipts_verified"] is True
        assert body["onchain_view_executed"] is False


# 3. complete, coherent result -> RESOLVE
def test_complete_result_resolves() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(client)
        assert decision["state"] == "RESOLVE"
        assert decision["resolved_selection"] == "HOME"
        assert decision["receipt_sha256"]


# 4. missing proof -> WAIT_FOR_PROOF
def test_missing_proof_waits() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(client, proof_status="MISSING", proof_reference=None, root_status="MISSING")
        assert decision["state"] == "WAIT_FOR_PROOF"


# 5. root mismatch -> DISPUTE
def test_root_mismatch_disputes() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(client, root_status="MISMATCH")
        assert decision["state"] == "DISPUTE"
        assert "onchain_root_mismatch" in decision["reasons"]


# 5b. whitespace-padded equal roots are non-canonical -> DISPUTE, never RESOLVE
def test_whitespace_padded_equal_roots_dispute() -> None:
    padded = " " + ROOT_HEX
    with TestClient(create_app()) as client:
        decision = _resolve_state(client, expected_root=padded, observed_root=padded)
        assert decision["state"] == "DISPUTE"
        assert decision["state"] != "RESOLVE"
        assert "declared_root_values_conflict" in decision["reasons"]


# 6. LIVE fixture -> PENDING_FINALITY
def test_live_fixture_pending_finality() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(
            client,
            fixture_status="LIVE",
            home_score=1,
            away_score=0,
            declared_result="HOME",
            proof_status="UNVERIFIED",
            root_status="UNVERIFIED",
            proof_reference=None,
            expected_root=None,
            observed_root=None,
        )
        assert decision["state"] == "PENDING_FINALITY"


# 7. scheduled fixture -> OPEN
def test_scheduled_fixture_open() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(
            client,
            fixture_status="SCHEDULED",
            home_score=None,
            away_score=None,
            declared_result=None,
            proof_status="MISSING",
            root_status="MISSING",
            proof_reference=None,
            expected_root=None,
            observed_root=None,
        )
        assert decision["state"] == "OPEN"


# 8. original receipt PASS, tampered receipt FAIL
def test_receipt_verification_pass_and_fail() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(client)
        original = client.post("/api/verify-receipt", json=decision)
        assert original.json()["status"] == "PASS"

        tampered = copy.deepcopy(decision)
        tampered["receipt_payload"]["state"] = "DISPUTE"
        broken = client.post("/api/verify-receipt", json=tampered)
        assert broken.json()["status"] == "FAIL"


# 9. invalid input and extra fields rejected
def test_invalid_and_extra_fields_rejected() -> None:
    with TestClient(create_app()) as client:
        # unknown top-level field
        extra_top = copy.deepcopy(BASE_REQUEST)
        extra_top["surprise"] = 1
        assert client.post("/api/resolve", json=extra_top).status_code == 422

        # unknown field inside evidence
        extra_evidence = copy.deepcopy(BASE_REQUEST)
        extra_evidence["evidence"]["surprise"] = 1
        assert client.post("/api/resolve", json=extra_evidence).status_code == 422

        # unknown field inside market
        extra_market = copy.deepcopy(BASE_REQUEST)
        extra_market["market"]["surprise"] = 1
        assert client.post("/api/resolve", json=extra_market).status_code == 422

        # wrong type
        wrong_type = copy.deepcopy(BASE_REQUEST)
        wrong_type["evidence"]["home_score"] = "two"
        assert client.post("/api/resolve", json=wrong_type).status_code == 422

        # missing required section
        missing = {"market": BASE_REQUEST["market"]}
        assert client.post("/api/resolve", json=missing).status_code == 422

        # invalid market_type literal
        bad_literal = copy.deepcopy(BASE_REQUEST)
        bad_literal["market"]["market_type"] = "OVER_UNDER"
        assert client.post("/api/resolve", json=bad_literal).status_code == 422

        # domain rule violation (negative score) rejected via 422
        negative = copy.deepcopy(BASE_REQUEST)
        negative["evidence"]["home_score"] = -1
        assert client.post("/api/resolve", json=negative).status_code == 422

        # fewer than two unique selections rejected via 422
        few_selections = copy.deepcopy(BASE_REQUEST)
        few_selections["market"]["selections"] = ["HOME"]
        assert client.post("/api/resolve", json=few_selections).status_code == 422


# 10. responses carry no secrets or .env values
def test_responses_contain_no_secrets() -> None:
    secret = "FINALITYGATE_ENV_SECRET_MUST_NOT_LEAK_9999"
    os.environ["TXLINE_GUEST_JWT"] = secret
    os.environ["TXLINE_API_TOKEN"] = secret
    try:
        with TestClient(create_app()) as client:
            decision = _resolve_state(client)
            texts = [
                client.get("/").text,
                client.get("/api/health").text,
                client.get("/api/status").text,
                client.get("/api/demo").text,
                json.dumps(decision),
            ]
        for text in texts:
            assert secret not in text
            assert "guest_jwt" not in text.lower()
            assert "api_token" not in text.lower()
    finally:
        os.environ.pop("TXLINE_GUEST_JWT", None)
        os.environ.pop("TXLINE_API_TOKEN", None)


# 11. security headers
def test_security_headers_present() -> None:
    with TestClient(create_app()) as client:
        dashboard = client.get("/")
        api = client.get("/api/status")
    assert dashboard.headers["X-Content-Type-Options"] == "nosniff"
    assert dashboard.headers["X-Frame-Options"] == "DENY"
    assert dashboard.headers["Referrer-Policy"] == "no-referrer"
    permissions = dashboard.headers["Permissions-Policy"]
    for feature in ("camera=()", "microphone=()", "geolocation=()", "payment=()"):
        assert feature in permissions
    assert api.headers["Cache-Control"] == "no-store"


# Dashboard must use relative API links so it works both standalone at "/" and
# mounted at "/finalitygate/".
def test_dashboard_uses_relative_api_links() -> None:
    with TestClient(create_app()) as client:
        text = client.get("/").text
    # No absolute /api/... links (neither quote style).
    assert 'href="/api/' not in text
    assert "href='/api/" not in text
    # Relative links are present.
    for endpoint in ("api/health", "api/status", "api/demo", "api/docs"):
        assert f"href='{endpoint}'" in text or f'href="{endpoint}"' in text


# Strict types: primitives must not be silently coerced.
def test_home_score_string_rejected() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/resolve", json=_request(home_score="2")).status_code == 422


def test_home_score_bool_rejected() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/resolve", json=_request(home_score=True)).status_code == 422


def test_numeric_market_id_rejected() -> None:
    with TestClient(create_app()) as client:
        payload = copy.deepcopy(BASE_REQUEST)
        payload["market"]["market_id"] = 123
        assert client.post("/api/resolve", json=payload).status_code == 422


def test_numeric_selection_rejected() -> None:
    with TestClient(create_app()) as client:
        payload = copy.deepcopy(BASE_REQUEST)
        payload["market"]["selections"] = [123, "DRAW", "AWAY"]
        assert client.post("/api/resolve", json=payload).status_code == 422


def test_numeric_fixture_status_and_proof_reference_rejected() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/resolve", json=_request(fixture_status=1)).status_code == 422
        assert client.post("/api/resolve", json=_request(proof_reference=7)).status_code == 422


def test_valid_iso_observed_at_still_resolves() -> None:
    with TestClient(create_app()) as client:
        decision = _resolve_state(client, observed_at="2026-07-10T12:00:00Z")
        assert decision["state"] == "RESOLVE"
        assert decision["resolved_selection"] == "HOME"


# 12. determinism between two independent runs
def test_demo_is_deterministic_between_runs() -> None:
    with TestClient(create_app()) as client_a:
        demo_a = client_a.get("/api/demo").json()
    with TestClient(create_app()) as client_b:
        demo_b = client_b.get("/api/demo").json()
    assert json.dumps(demo_a, sort_keys=True) == json.dumps(demo_b, sort_keys=True)

    # And two resolves of the same input yield an identical receipt hash.
    with TestClient(create_app()) as client:
        first = _resolve_state(client)
        second = _resolve_state(client)
    assert first["receipt_sha256"] == second["receipt_sha256"]
