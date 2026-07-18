"""In-process smoke test for the FinalityGate web app.

Exercises every public route through an ASGI TestClient without any network
access, then prints a JSON PASS/FAIL report. Used from source and from an
isolated wheel by the local gate.
"""

from __future__ import annotations

import copy
import json

from fastapi.testclient import TestClient

from .app import ONCHAIN_VIEW_EXECUTED, REQUIRED_STATES, create_app

_ROOT = "cc" * 32

RESOLVE_REQUEST = {
    "market": {
        "market_id": "market-smoke-1",
        "fixture_id": "fixture-smoke-1",
        "market_type": "MATCH_RESULT",
        "selections": ["HOME", "DRAW", "AWAY"],
        "policy_version": "finalitygate-v1",
    },
    "evidence": {
        "fixture_id": "fixture-smoke-1",
        "fixture_status": "FINAL",
        "home_score": 2,
        "away_score": 1,
        "declared_result": "HOME",
        "observed_at": "2026-07-10T12:00:00Z",
        "proof_status": "VALID",
        "root_status": "MATCH",
        "proof_reference": "proof-smoke-1",
        "expected_root": _ROOT,
        "observed_root": _ROOT,
        "source_fingerprint": "txline-schema-v1",
    },
}

SECRET_MARKER = "FINALITYGATE_SMOKE_SECRET_MUST_NOT_LEAK"


def main() -> int:
    app = create_app()
    checks: dict[str, bool] = {}
    with TestClient(app) as client:
        health = client.get("/api/health")
        status = client.get("/api/status")
        demo = client.get("/api/demo")
        dashboard = client.get("/")
        docs = client.get("/api/docs")
        openapi = client.get("/api/openapi.json")

        resolve = client.post("/api/resolve", json=RESOLVE_REQUEST)
        decision = resolve.json() if resolve.status_code == 200 else {}

        verify_original = client.post("/api/verify-receipt", json=decision)

        tampered = copy.deepcopy(decision)
        if isinstance(tampered.get("receipt_payload"), dict):
            tampered["receipt_payload"]["state"] = "RESOLVE_TAMPERED"
        verify_tampered = client.post("/api/verify-receipt", json=tampered)

        # Unknown field must be rejected, not ignored.
        bad_extra = copy.deepcopy(RESOLVE_REQUEST)
        bad_extra["evidence"]["unexpected_field"] = "nope"
        reject_extra = client.post("/api/resolve", json=bad_extra)

        # Wrong type must be rejected.
        bad_type = copy.deepcopy(RESOLVE_REQUEST)
        bad_type["evidence"]["home_score"] = "two"
        reject_type = client.post("/api/resolve", json=bad_type)

        serialized = " ".join(
            r.text for r in (health, status, demo, dashboard, resolve, verify_original)
        )

        state_counts = demo.json().get("state_counts", {}) if demo.status_code == 200 else {}
        health_body = health.json() if health.status_code == 200 else {}
        status_body = status.json() if status.status_code == 200 else {}

        checks = {
            "health_200": health.status_code == 200,
            "status_200": status.status_code == 200,
            "demo_200": demo.status_code == 200,
            "dashboard_200": dashboard.status_code == 200,
            "docs_200": docs.status_code == 200,
            "openapi_200": openapi.status_code == 200,
            "health_pass": health_body.get("status") == "PASS",
            "all_states_present": set(REQUIRED_STATES).issubset(state_counts),
            "resolve_state_resolve": decision.get("state") == "RESOLVE",
            "resolve_selection_home": decision.get("resolved_selection") == "HOME",
            "receipt_original_pass": verify_original.json().get("status") == "PASS",
            "receipt_tampered_fail": verify_tampered.json().get("status") == "FAIL",
            "extra_field_rejected": reject_extra.status_code == 422,
            "wrong_type_rejected": reject_type.status_code == 422,
            "onchain_view_false": status_body.get("onchain_view_executed") is False
            and ONCHAIN_VIEW_EXECUTED is False,
            "security_header_present": dashboard.headers.get("X-Frame-Options") == "DENY"
            and status.headers.get("Cache-Control") == "no-store",
            "no_secret_leak": SECRET_MARKER not in serialized,
        }

    payload = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
