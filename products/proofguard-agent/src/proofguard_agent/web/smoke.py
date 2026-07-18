from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from .app import create_app
from .runtime import ProofGuardRuntime, RuntimeConfig


def main() -> int:
    config = RuntimeConfig(requested_mode="REPLAY", auto_start=False, poll_seconds=1.0)
    runtime = ProofGuardRuntime(config)
    asyncio.run(runtime.cycle_once())
    app = create_app(runtime=runtime)

    checks: dict[str, bool] = {}
    with TestClient(app) as client:
        health = client.get("/api/health")
        snapshot = client.get("/api/snapshot")
        dashboard = client.get("/")
        receipts = client.get("/api/receipts?limit=5")
        serialized = snapshot.text.lower()
        checks = {
            "health_200": health.status_code == 200,
            "snapshot_200": snapshot.status_code == 200,
            "dashboard_200": dashboard.status_code == 200,
            "receipts_200": receipts.status_code == 200,
            "replay_explicit": snapshot.json().get("source_mode") == "REPLAY",
            "decision_present": snapshot.json().get("latest_decision") is not None,
            "simulation_only": snapshot.json().get("safety", {}).get("simulation_only") is True,
            "raw_payload_not_persisted": snapshot.json().get("safety", {}).get("raw_payload_persisted") is False,
            "raw_credential_fields_absent": '"guest_jwt":' not in serialized and '"api_token":' not in serialized,
            "dashboard_labels_mode": "REPLAY_FALLBACK" in dashboard.text and "Simulation only" in dashboard.text,
        }

    payload = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
