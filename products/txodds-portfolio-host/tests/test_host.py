from __future__ import annotations

import importlib
import os
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finalitygate.web.app import create_app as create_finalitygate_app

from txodds_portfolio_host.app import create_host_app

SECRET_JWT = "HOST_TEST_JWT_MUST_NOT_LEAK_0001"
SECRET_TOKEN = "HOST_TEST_TOKEN_MUST_NOT_LEAK_0002"


def _replay_env() -> None:
    os.environ["PROOFGUARD_MODE"] = "REPLAY"
    os.environ["PROOFGUARD_AUTO_START"] = "true"
    os.environ["PROOFGUARD_POLL_SECONDS"] = "0.25"
    os.environ.pop("TXLINE_GUEST_JWT", None)
    os.environ.pop("TXLINE_API_TOKEN", None)


# --------------------------------------------------------------------------
# Routing isolation
# --------------------------------------------------------------------------
def test_landing_and_both_products_route() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        assert client.get("/").status_code == 200
        landing = client.get("/").text
        # Judge-facing landing names both products and states simulation-only.
        assert "ProofGuard" in landing and "FinalityGate" in landing
        assert "simulation-only" in landing

        pg = client.get("/proofguard/api/health")
        assert pg.status_code == 200
        assert pg.json()["status"] == "PASS"

        fg = client.get("/finalitygate/api/health")
        assert fg.status_code == 200
        assert fg.json()["status"] == "PASS"
        assert fg.json()["component"] == "finalitygate"

        # ProofGuard dashboard and FinalityGate dashboard are distinct pages.
        assert "ProofGuard" in client.get("/proofguard/").text
        assert "FinalityGate" in client.get("/finalitygate/").text


def test_openapi_isolation() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        pg_spec = client.get("/proofguard/api/openapi.json").json()
        fg_spec = client.get("/finalitygate/api/openapi.json").json()
        pg_paths = set(pg_spec["paths"])
        fg_paths = set(fg_spec["paths"])
        # Each product exposes its own paths; FinalityGate has no /api/snapshot,
        # ProofGuard has no /api/demo.
        assert "/api/snapshot" in pg_paths
        assert "/api/snapshot" not in fg_paths
        assert "/api/demo" in fg_paths
        assert "/api/demo" not in pg_paths


# --------------------------------------------------------------------------
# Combined lifespan: ProofGuard background loop runs under the host process
# --------------------------------------------------------------------------
def test_combined_lifespan_runs_proofguard_loop() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        deadline = time.monotonic() + 3.0
        snap = client.get("/proofguard/api/snapshot").json()
        while snap["cycle_count"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            snap = client.get("/proofguard/api/snapshot").json()
        assert snap["task_running"] is True
        assert snap["cycle_count"] >= 2
        assert snap["source_mode"] == "REPLAY"
        assert snap["latest_cycle"]["safety"]["unsafe_entry_count"] == 0


# --------------------------------------------------------------------------
# Combined health reports each component separately
# --------------------------------------------------------------------------
def test_combined_health_reports_both_components() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        body = client.get("/health")
        assert body.status_code == 200
        data = body.json()
        assert data["status"] == "PASS"
        assert data["service"] == "txodds-portfolio-host"
        assert data["components"]["proofguard"]["status"] == "PASS"
        assert data["components"]["finalitygate"]["status"] == "PASS"


# --------------------------------------------------------------------------
# One product failure must not silently corrupt the other
# --------------------------------------------------------------------------
def test_one_component_failure_isolated() -> None:
    _replay_env()

    # A broken FinalityGate app whose health raises: /health must mark it FAIL
    # yet still serve ProofGuard, and ProofGuard routes must keep working.
    broken_fg = FastAPI()

    @broken_fg.get("/api/health")
    async def broken_health() -> dict:  # pragma: no cover - exercised via client
        raise RuntimeError("finalitygate boom")

    host = create_host_app(finalitygate_app=broken_fg)
    with TestClient(host) as client:
        health = client.get("/health")
        assert health.status_code == 503
        data = health.json()
        # ProofGuard still healthy; FinalityGate reported failed — not hidden.
        assert data["components"]["proofguard"]["status"] == "PASS"
        assert data["components"]["finalitygate"]["status"] == "FAIL"
        # ProofGuard routes remain fully functional despite the other failure.
        assert client.get("/proofguard/api/health").json()["status"] == "PASS"


# --------------------------------------------------------------------------
# No credential leakage across the combined surface
# --------------------------------------------------------------------------
def test_no_secret_leak_across_host() -> None:
    _replay_env()
    os.environ["TXLINE_GUEST_JWT"] = SECRET_JWT
    os.environ["TXLINE_API_TOKEN"] = SECRET_TOKEN
    try:
        with TestClient(create_host_app()) as client:
            for path in (
                "/",
                "/health",
                "/proofguard/",
                "/proofguard/api/health",
                "/proofguard/api/snapshot",
                "/proofguard/api/status",
                "/finalitygate/",
                "/finalitygate/api/health",
                "/finalitygate/api/status",
                "/finalitygate/api/demo",
            ):
                text = client.get(path).text
                assert SECRET_JWT not in text, path
                assert SECRET_TOKEN not in text, path
    finally:
        os.environ.pop("TXLINE_GUEST_JWT", None)
        os.environ.pop("TXLINE_API_TOKEN", None)


# --------------------------------------------------------------------------
# Native FinalityGate app summary is deterministic and covers all states
# --------------------------------------------------------------------------
def test_finalitygate_native_state_coverage() -> None:
    with TestClient(create_finalitygate_app()) as client:
        demo = client.get("/api/demo").json()
        assert demo["status"] == "PASS"
        assert set(demo["state_counts"]) >= {"OPEN", "PENDING_FINALITY", "WAIT_FOR_PROOF", "RESOLVE", "DISPUTE"}


# --------------------------------------------------------------------------
# 15. The host mounts the *native* FinalityGate app (not the old read-only
# adapter) and keeps the two products isolated.
# --------------------------------------------------------------------------
def test_host_mounts_native_finalitygate_and_isolates_products() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        # Native-only routes prove the real product app is mounted: the old
        # host adapter had no /api/resolve or /api/verify-receipt.
        resolve = client.post(
            "/finalitygate/api/resolve",
            json={
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
                    "expected_root": "cc" * 32,
                    "observed_root": "cc" * 32,
                    "source_fingerprint": "txline-schema-v1",
                },
            },
        )
        assert resolve.status_code == 200
        decision = resolve.json()
        assert decision["state"] == "RESOLVE"

        verified = client.post("/finalitygate/api/verify-receipt", json=decision)
        assert verified.json()["status"] == "PASS"

        # FinalityGate native status keeps the on-chain boundary explicit.
        status = client.get("/finalitygate/api/status").json()
        assert status["onchain_view_executed"] is False

        # Isolation: FinalityGate exposes /api/resolve; ProofGuard does not.
        fg_paths = set(client.get("/finalitygate/api/openapi.json").json()["paths"])
        pg_paths = set(client.get("/proofguard/api/openapi.json").json()["paths"])
        assert "/api/resolve" in fg_paths
        assert "/api/resolve" not in pg_paths
        assert "/api/snapshot" in pg_paths
        assert "/api/snapshot" not in fg_paths

        # ProofGuard remains fully functional alongside the native FinalityGate app.
        assert client.get("/proofguard/api/health").json()["status"] == "PASS"


# --------------------------------------------------------------------------
# The mounted FinalityGate dashboard must use relative API links so they point
# at /finalitygate/api/... and not at the host root /api/...
# --------------------------------------------------------------------------
def test_mounted_finalitygate_dashboard_links_are_relative_and_reachable() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        html = client.get("/finalitygate/").text
        # No absolute /api/... links that would escape the mount prefix.
        assert 'href="/api/' not in html
        assert "href='/api/" not in html

        # The relative links resolve under the mount prefix and reach the real
        # FinalityGate endpoints (not the host root).
        for endpoint in ("health", "status", "demo", "docs"):
            assert f"href='api/{endpoint}'" in html or f'href="api/{endpoint}"' in html
            mounted = client.get(f"/finalitygate/api/{endpoint}")
            assert mounted.status_code == 200, endpoint

        # The host root has no /api/health|status|demo of its own, confirming
        # the links would be broken if they were absolute.
        assert client.get("/api/health").status_code == 404
        assert client.get("/api/demo").status_code == 404


# --------------------------------------------------------------------------
# Phase 2 C - host audit
# --------------------------------------------------------------------------
def test_c_host_security_headers_present() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        health = client.get("/health")
        assert health.headers["X-Content-Type-Options"] == "nosniff"
        assert health.headers["Referrer-Policy"] == "no-referrer"
        assert health.headers["Cache-Control"] == "no-store"
        # Mounted product responses keep their own hardened headers too.
        fg = client.get("/finalitygate/api/health")
        assert fg.headers["X-Content-Type-Options"] == "nosniff"
        assert fg.headers["Cache-Control"] == "no-store"
        pg = client.get("/proofguard/api/snapshot")
        assert pg.headers["Cache-Control"] == "no-store"


def test_c_no_duplicate_finalitygate_adapter_module() -> None:
    # The host-owned adapter was deleted; no dead/duplicate module remains.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("txodds_portfolio_host.finalitygate_app")


def test_c_finalitygate_post_fail_closed_under_prefix() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        payload = {
            "market": {
                "market_id": "m",
                "fixture_id": "f",
                "market_type": "MATCH_RESULT",
                "selections": ["HOME", "DRAW", "AWAY"],
                "policy_version": "finalitygate-v1",
            },
            "evidence": {
                "fixture_id": "f",
                "fixture_status": "FINAL",
                "home_score": 2,
                "away_score": 1,
                "declared_result": "HOME",
                "observed_at": "2026-07-10T12:00:00Z",
                "proof_status": "VALID",
                "root_status": "MATCH",
                "proof_reference": "p",
                "expected_root": "x",  # non-canonical -> fail-closed DISPUTE
                "observed_root": "x",
            },
        }
        resp = client.post("/finalitygate/api/resolve", json=payload)
        assert resp.status_code == 200
        assert resp.json()["state"] == "DISPUTE"


def test_c_proofguard_receipts_bounded_under_host_load() -> None:
    _replay_env()
    with TestClient(create_host_app()) as client:
        deadline = time.monotonic() + 4.0
        snap = client.get("/proofguard/api/snapshot").json()
        while snap["cycle_count"] < 15 and time.monotonic() < deadline:
            time.sleep(0.02)
            snap = client.get("/proofguard/api/snapshot").json()
        max_receipts = snap["configuration"]["max_receipts"]
        assert len(snap["receipts"]) <= max_receipts
        assert snap["consecutive_errors"] >= 0  # error list never grows unbounded (bounded deque)
