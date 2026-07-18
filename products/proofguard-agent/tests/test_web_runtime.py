from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from proofguard_agent.txline import TxLineConfig, TxLineError
from proofguard_agent.web.app import create_app
from proofguard_agent.web.runtime import REPLAY_SCENARIO, ProofGuardRuntime, RuntimeConfig

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


class FakeTxLineClient:
    def __init__(self, _: TxLineConfig) -> None:
        pass

    def odds_snapshot(self, fixture_id: str) -> dict[str, object]:
        assert fixture_id == "fixture-live-1"
        return {
            "data": [
                {
                    "fixtureId": fixture_id,
                    "market": "MATCH_RESULT",
                    "selection": "HOME",
                    "stablePrice": 0.45,
                    "timestamp": "2026-07-12T11:59:50Z",
                    "proofRef": "proof-live-1",
                    "messageId": "live-home",
                },
                {
                    "fixtureId": fixture_id,
                    "market": "MATCH_RESULT",
                    "selection": "DRAW",
                    "stablePrice": 0.30,
                    "timestamp": "2026-07-12T11:59:50Z",
                    "proofRef": "proof-live-1",
                    "messageId": "live-draw",
                },
                {
                    "fixtureId": fixture_id,
                    "market": "MATCH_RESULT",
                    "selection": "AWAY",
                    "stablePrice": 0.25,
                    "timestamp": "2026-07-12T11:59:50Z",
                    "proofRef": "proof-live-1",
                    "messageId": "live-away",
                },
            ]
        }

    def scores_snapshot(self, fixture_id: str) -> dict[str, object]:
        assert fixture_id == "fixture-live-1"
        return {
            "scores": [
                {
                    "fixtureId": fixture_id,
                    "status": "LIVE",
                    "homeScore": 1,
                    "awayScore": 0,
                    "timestamp": "2026-07-12T12:00:00Z",
                }
            ]
        }


class ScoreFailingTxLineClient(FakeTxLineClient):
    def __init__(self, config: TxLineConfig) -> None:
        self.config = config

    def scores_snapshot(self, fixture_id: str) -> dict[str, object]:
        assert fixture_id == "fixture-live-1"
        raise TxLineError(f"scores rejected {self.config.guest_jwt} {self.config.api_token}")


class FailingTxLineClient:
    def __init__(self, config: TxLineConfig) -> None:
        self.config = config

    def odds_snapshot(self, _: str) -> dict[str, object]:
        raise TxLineError(f"provider rejected {self.config.guest_jwt} {self.config.api_token}")


def replay_config(*, max_receipts: int = 100) -> RuntimeConfig:
    return RuntimeConfig(
        requested_mode="REPLAY",
        auto_start=False,
        poll_seconds=1.0,
        max_receipts=max_receipts,
        txline=TxLineConfig(origin="https://example.com"),
    )


def live_config(*, fallback: bool = True) -> RuntimeConfig:
    return RuntimeConfig(
        requested_mode="LIVE",
        fixture_id="fixture-live-1",
        model_probabilities={"HOME": 0.64, "DRAW": 0.24, "AWAY": 0.12},
        models_explicitly_configured=True,
        poll_seconds=1.0,
        with_scores=True,
        replay_fallback=fallback,
        auto_start=False,
        max_receipts=20,
        txline=TxLineConfig(
            origin="https://example.com",
            guest_jwt="guest-secret-value",
            api_token="api-secret-value",
        ),
    )


def incomplete_live_config(*, fallback: bool) -> RuntimeConfig:
    return RuntimeConfig(
        requested_mode="LIVE",
        fixture_id=None,
        models_explicitly_configured=False,
        replay_fallback=fallback,
        auto_start=False,
        txline=TxLineConfig(origin="https://example.com"),
    )


def test_replay_cycle_is_explicit_and_preserves_safety_invariant() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)

    cycle = asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())

    assert cycle["status"] == "PASS"
    assert cycle["safety"]["unsafe_entry_count"] == 0
    assert snapshot["source_mode"] == "REPLAY"
    assert snapshot["latest_decision"]["action"] == "ENTER"
    assert snapshot["safety"]["simulation_only"] is True
    assert snapshot["safety"]["real_money_execution"] is False


def test_decision_distribution_reflects_receipt_history_and_safety() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
    for _ in range(6):
        asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())

    dist = snapshot["decision_distribution"]
    # Total equals the receipt history length.
    assert dist["total_decisions"] == len(snapshot["receipts"])
    assert dist["total_decisions"] > 0
    # Counts partition the total exactly.
    assert sum(dist["by_action"].values()) == dist["total_decisions"]
    assert sum(dist["by_integrity_gate"].values()) == dist["total_decisions"]
    # The advertised safety invariant holds and is computed from the receipts.
    assert dist["unsafe_enter_count"] == 0
    assert dist["safety_invariant_holds"] is True


def test_integrity_impact_quantifies_blocked_exploits_vs_naive_agent() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
    # Run a full scripted match so the stale/corrupt attack cycles occur.
    for _ in range(12):
        asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())

    impact = snapshot["integrity_impact"]
    # The replay includes signals with a positive edge that fail the integrity
    # gate (stale feed, corrupted book) — a naive edge-only agent would enter
    # them; ProofGuard must have blocked at least one.
    assert impact["integrity_exploits_blocked"] >= 1
    assert impact["paper_exposure_at_risk_prevented"] >= 0.0
    # Every blocked exploit is also, by definition, a signal a naive agent enters.
    assert impact["naive_entry_signals"] >= impact["integrity_exploits_blocked"]
    # Consistency with the safety invariant: nothing unsafe actually entered.
    assert snapshot["decision_distribution"]["safety_invariant_holds"] is True


def test_replay_scenario_tells_full_match_story_and_stays_safe() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)

    actions: list[str] = []
    integrity: list[str] = []
    minutes: list[int] = []
    for _ in range(len(REPLAY_SCENARIO)):
        asyncio.run(runtime.cycle_once())
        snap = asyncio.run(runtime.snapshot())
        decision = snap["latest_decision"]
        actions.append(decision["action"])
        integrity.append(decision["integrity"]["decision"])
        minutes.append(snap["source"]["match_minute"])
        # Narrative + scenario position are always present and safety holds.
        assert snap["source"]["narrative"]
        assert 1 <= snap["source"]["scenario_step"] <= len(REPLAY_SCENARIO)
        assert snap["latest_cycle"]["safety"]["unsafe_entry_count"] == 0

    # The scripted match exercises every action and every integrity verdict.
    assert {"ENTER", "HOLD", "REJECT", "CLOSE"} <= set(actions)
    assert {"PASS", "REVIEW", "BLOCK"} <= set(integrity)
    # The clock runs 1' -> 90' and full time closes every open paper position.
    assert minutes[0] == 1 and minutes[-1] == 90
    final = asyncio.run(runtime.snapshot())
    assert final["portfolio"]["open_position_count"] == 0
    assert final["latest_decision"]["action"] == "CLOSE"


def test_live_cycle_uses_normalized_txline_input_and_does_not_expose_secrets() -> None:
    runtime = ProofGuardRuntime(live_config(), client_factory=FakeTxLineClient, clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())
    serialized = json.dumps(snapshot)

    assert snapshot["source_mode"] == "LIVE"
    assert snapshot["source"]["provider"] == "TxLINE"
    assert snapshot["source"]["raw_payload_persisted"] is False
    assert snapshot["source"]["score_summary"]["status"] == "AVAILABLE"
    assert len(snapshot["market"]) == 3
    assert snapshot["latest_cycle"]["safety"]["unsafe_entry_count"] == 0
    assert "guest-secret-value" not in serialized
    assert "api-secret-value" not in serialized
    assert '"guest_jwt":' not in serialized
    assert '"api_token":' not in serialized


def test_optional_score_failure_preserves_live_odds_and_redacts_warning() -> None:
    runtime = ProofGuardRuntime(live_config(), client_factory=ScoreFailingTxLineClient, clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())
    serialized = json.dumps(snapshot)
    score_summary = snapshot["source"]["score_summary"]

    assert snapshot["source_mode"] == "LIVE"
    assert snapshot["status"] == "RUNNING"
    assert snapshot["last_error"] is None
    assert snapshot["consecutive_errors"] == 0
    assert len(snapshot["market"]) == 3
    assert snapshot["latest_cycle"]["safety"]["unsafe_entry_count"] == 0
    assert score_summary["requested"] is True
    assert score_summary["available"] is False
    assert score_summary["status"] == "UNAVAILABLE"
    assert "<redacted>" in score_summary["error"]
    assert "guest-secret-value" not in serialized
    assert "api-secret-value" not in serialized


def test_provider_failure_falls_back_redacts_credentials_and_preserves_error_count() -> None:
    runtime = ProofGuardRuntime(live_config(fallback=True), client_factory=FailingTxLineClient, clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())
    serialized = json.dumps(snapshot)

    assert snapshot["source_mode"] == "REPLAY_FALLBACK"
    assert snapshot["status"] == "DEGRADED"
    assert snapshot["consecutive_errors"] == 1
    assert "<redacted>" in snapshot["last_error"]
    assert "guest-secret-value" not in serialized
    assert "api-secret-value" not in serialized
    assert snapshot["latest_cycle"]["safety"]["unsafe_entry_count"] == 0


def test_provider_recovery_resets_state_after_replay_fallback() -> None:
    class _RecoveringFactory:
        """Fail the first odds_snapshot, then serve a valid LIVE cycle."""

        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, config: TxLineConfig):
            self.calls += 1
            if self.calls == 1:
                return FailingTxLineClient(config)
            return FakeTxLineClient(config)

    runtime = ProofGuardRuntime(live_config(fallback=True), client_factory=_RecoveringFactory(), clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    degraded = asyncio.run(runtime.snapshot())

    assert degraded["source_mode"] == "REPLAY_FALLBACK"
    assert degraded["status"] == "DEGRADED"
    assert degraded["consecutive_errors"] == 1
    assert degraded["last_error"] is not None
    assert "<redacted>" in degraded["last_error"]

    asyncio.run(runtime.cycle_once())
    recovered = asyncio.run(runtime.snapshot())
    serialized = json.dumps(recovered)

    assert recovered["source_mode"] == "LIVE"
    assert recovered["status"] == "RUNNING"
    assert recovered["consecutive_errors"] == 0
    assert recovered["last_error"] is None
    assert len(recovered["market"]) == 3
    assert recovered["latest_cycle"]["safety"]["unsafe_entry_count"] == 0
    assert "guest-secret-value" not in serialized
    assert "api-secret-value" not in serialized
    assert '"guest_jwt":' not in serialized
    assert '"api_token":' not in serialized


def test_receipt_history_is_bounded() -> None:
    runtime = ProofGuardRuntime(replay_config(max_receipts=10), clock=lambda: NOW)

    for _ in range(16):
        asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())

    assert len(snapshot["receipts"]) == 10
    assert snapshot["cycle_count"] == 16


def test_public_api_and_dashboard_are_working() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
    asyncio.run(runtime.cycle_once())
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        health = client.get("/api/health")
        snapshot = client.get("/api/snapshot")
        status = client.get("/api/status")
        market = client.get("/api/market/latest")
        decision = client.get("/api/decision/latest")
        positions = client.get("/api/positions")
        receipts = client.get("/api/receipts?limit=1")
        verify = client.get("/api/receipts/verify")
        model = client.get("/api/model/preview")
        dashboard = client.get("/")

    assert health.status_code == 200
    assert snapshot.status_code == 200
    assert status.status_code == 200
    assert market.status_code == 200
    assert decision.status_code == 200
    assert positions.status_code == 200
    assert receipts.status_code == 200
    assert verify.status_code == 200
    assert model.status_code == 200
    assert dashboard.status_code == 200
    assert snapshot.json()["source_mode"] == "REPLAY"
    assert receipts.json()["count"] == 1
    verify_body = verify.json()
    assert verify_body["status"] == "PASS"
    assert verify_body["window"] >= 1
    assert model.json()["timeline"][-1]["minute"] == 90
    assert "ProofGuard" in dashboard.text
    assert "Simulation only" in dashboard.text
    assert dashboard.headers["X-Frame-Options"] == "DENY"
    assert snapshot.headers["Cache-Control"] == "no-store"


def test_playground_and_simulate_endpoint() -> None:
    runtime = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
    app = create_app(runtime=runtime)
    with TestClient(app) as client:
        page = client.get("/playground")
        assert page.status_code == 200
        assert "Integrity Gate Playground" in page.text
        assert "fetch('api/simulate" in page.text  # relative, mount-safe

        # The attack scenario must be BLOCKED and rejected, never entered.
        attack = client.get("/api/simulate?scenario=corrupt_block").json()
        assert attack["decision"]["integrity"]["decision"] == "BLOCK"
        assert attack["decision"]["action"] == "REJECT"

        # A clean value scenario enters.
        clean = client.get("/api/simulate?scenario=clean_value").json()
        assert clean["decision"]["integrity"]["decision"] == "PASS"
        assert clean["decision"]["action"] == "ENTER"

        # Unknown scenario -> 404.
        assert client.get("/api/simulate?scenario=nope").status_code == 404


def test_explicit_live_without_readiness_uses_labelled_fallback_and_failing_health() -> None:
    runtime = ProofGuardRuntime(incomplete_live_config(fallback=True), clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())
    health = asyncio.run(runtime.health())

    assert snapshot["configuration"]["requested_mode"] == "LIVE"
    assert snapshot["configuration"]["live_ready"] is False
    assert snapshot["configuration"]["missing_live_requirements"] == [
        "fixture_id",
        "model_probabilities",
        "txline_guest_jwt",
        "txline_api_token",
    ]
    assert snapshot["source_mode"] == "REPLAY_FALLBACK"
    assert snapshot["status"] == "DEGRADED"
    assert snapshot["consecutive_errors"] == 1
    assert "explicit LIVE mode is not ready" in snapshot["last_error"]
    assert health["status"] == "FAIL"


def test_explicit_live_without_readiness_and_without_fallback_enters_error() -> None:
    runtime = ProofGuardRuntime(incomplete_live_config(fallback=False), clock=lambda: NOW)

    with pytest.raises(RuntimeError, match="explicit LIVE mode is not ready"):
        asyncio.run(runtime.cycle_once())

    snapshot = asyncio.run(runtime.snapshot())
    health = asyncio.run(runtime.health())

    assert snapshot["source_mode"] == "ERROR"
    assert snapshot["status"] == "DEGRADED"
    assert snapshot["cycle_count"] == 0
    assert snapshot["consecutive_errors"] == 1
    assert health["status"] == "FAIL"


def test_auto_without_live_readiness_remains_explicit_replay() -> None:
    config = RuntimeConfig(
        requested_mode="AUTO",
        fixture_id=None,
        models_explicitly_configured=False,
        auto_start=False,
        txline=TxLineConfig(origin="https://example.com"),
    )
    runtime = ProofGuardRuntime(config, clock=lambda: NOW)

    asyncio.run(runtime.cycle_once())
    snapshot = asyncio.run(runtime.snapshot())
    health = asyncio.run(runtime.health())

    assert snapshot["configuration"]["requested_mode"] == "AUTO"
    assert snapshot["configuration"]["live_ready"] is False
    assert snapshot["source_mode"] == "REPLAY"
    assert snapshot["status"] == "RUNNING"
    assert snapshot["consecutive_errors"] == 0
    assert health["status"] == "PASS"
