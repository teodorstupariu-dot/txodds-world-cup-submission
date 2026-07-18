"""Adversarial regression tests for ProofGuard (audit Phase 2 B).

B1 - snapshot isolation (mutating a returned snapshot must not corrupt state).
B2 - concurrency and lifecycle.
B3 - error redaction and truncation.
B4 - receipt/safety invariants not already covered elsewhere.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from proofguard_agent.core import MarketEvent, ProofGuardAutonomousAgent
from proofguard_agent.txline import TxLineConfig, TxLineError
from proofguard_agent.web.runtime import ProofGuardRuntime, RuntimeConfig

NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
JWT = "GUEST_JWT_SECRET_MUST_NOT_LEAK_0001"
TOKEN = "API_TOKEN_SECRET_MUST_NOT_LEAK_0002"


def replay_config(*, max_receipts: int = 100) -> RuntimeConfig:
    return RuntimeConfig(
        requested_mode="REPLAY",
        auto_start=False,
        poll_seconds=0.05,
        max_receipts=max_receipts,
        txline=TxLineConfig(origin="https://example.com"),
    )


def secret_live_config() -> RuntimeConfig:
    return RuntimeConfig(
        requested_mode="LIVE",
        fixture_id="fixture-live-1",
        model_probabilities={"HOME": 0.64, "DRAW": 0.24, "AWAY": 0.12},
        models_explicitly_configured=True,
        poll_seconds=0.05,
        replay_fallback=True,
        auto_start=False,
        txline=TxLineConfig(origin="https://example.com", guest_jwt=JWT, api_token=TOKEN),
    )


class FailingTxLineClient:
    def __init__(self, config: TxLineConfig) -> None:
        self.config = config

    def odds_snapshot(self, _: str) -> dict[str, object]:
        raise TxLineError(f"provider rejected {self.config.guest_jwt} {self.config.api_token}")


# ---------------------------------------------------------------------------
# B1 - snapshot isolation
# ---------------------------------------------------------------------------
def test_b1_snapshot_mutation_does_not_corrupt_runtime() -> None:
    rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
    for _ in range(3):
        asyncio.run(rt.cycle_once())
    snap = asyncio.run(rt.snapshot())
    baseline = json.dumps(asyncio.run(rt.snapshot()), sort_keys=True)

    # Mutate every mutable field of the returned snapshot.
    snap["source"]["mode"] = "HACKED"
    snap["source"]["injected"] = True
    snap["market"].append({"injected": True})
    snap["market"][0]["selection"] = "HACKED"
    snap["latest_cycle"]["status"] = "HACKED"
    snap["latest_cycle"]["records"].append({"injected": True})
    snap["latest_decision"]["action"] = "HACKED"
    snap["receipts"].append({"injected": True})
    if snap["receipts"]:
        snap["receipts"][0]["action"] = "HACKED"
    snap["recent_errors"].append({"injected": True})
    snap["portfolio"]["total_exposure"] = 999
    snap["portfolio"]["positions"].append({"injected": True})

    after = json.dumps(asyncio.run(rt.snapshot()), sort_keys=True)
    assert after == baseline
    assert "HACKED" not in after
    assert "injected" not in after


# ---------------------------------------------------------------------------
# B2 - concurrency and lifecycle
# ---------------------------------------------------------------------------
def test_b2_concurrent_cycles_serialize_without_corruption() -> None:
    async def scenario() -> dict:
        rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
        await asyncio.gather(*(rt.cycle_once() for _ in range(20)))
        return await rt.snapshot()

    snap = asyncio.run(scenario())
    assert snap["cycle_count"] == 20
    assert snap["latest_cycle"]["safety"]["unsafe_entry_count"] == 0
    assert snap["consecutive_errors"] == 0


def test_b2_repeated_start_creates_no_duplicate_task() -> None:
    async def scenario() -> None:
        rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
        rt.start()
        task = rt._task
        rt.start()
        assert rt._task is task
        assert rt.task_running is True
        await rt.stop()

    asyncio.run(scenario())


def test_b2_repeated_stop_is_safe() -> None:
    async def scenario() -> None:
        rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
        await rt.stop()
        await rt.stop()
        assert rt.task_running is False

    asyncio.run(scenario())


def test_b2_start_stop_start_cycles_and_is_monotonic() -> None:
    async def wait_cycles(rt: ProofGuardRuntime, target: int) -> None:
        deadline = asyncio.get_event_loop().time() + 3.0
        while (await rt.snapshot())["cycle_count"] < target and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

    async def scenario() -> None:
        rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
        rt.start()
        await wait_cycles(rt, 1)
        first = (await rt.snapshot())["cycle_count"]
        assert first >= 1
        await rt.stop()
        assert rt.task_running is False
        rt.start()
        await wait_cycles(rt, first + 1)
        second = (await rt.snapshot())["cycle_count"]
        assert second > first  # monotonic across restart
        await rt.stop()

    asyncio.run(scenario())


def test_b2_receipts_bounded_and_cycle_count_monotonic() -> None:
    async def scenario() -> dict:
        rt = ProofGuardRuntime(replay_config(max_receipts=10), clock=lambda: NOW)
        counts = []
        for _ in range(25):
            await rt.cycle_once()
            counts.append((await rt.snapshot())["cycle_count"])
        assert counts == sorted(counts) and counts[-1] == 25
        return await rt.snapshot()

    snap = asyncio.run(scenario())
    assert len(snap["receipts"]) == 10
    assert snap["cycle_count"] == 25


def test_b2_provider_failure_then_recovery_cleans_state() -> None:
    class RecoveringFactory:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, config: TxLineConfig):
            self.calls += 1
            if self.calls == 1:
                return FailingTxLineClient(config)
            # import here to reuse the shared fake shape
            from tests.test_web_runtime import FakeTxLineClient

            return FakeTxLineClient(config)

    async def scenario() -> tuple[dict, dict]:
        rt = ProofGuardRuntime(secret_live_config(), client_factory=RecoveringFactory(), clock=lambda: NOW)
        await rt.cycle_once()
        degraded = await rt.snapshot()
        await rt.cycle_once()
        recovered = await rt.snapshot()
        return degraded, recovered

    degraded, recovered = asyncio.run(scenario())
    assert degraded["source_mode"] == "REPLAY_FALLBACK"
    assert degraded["consecutive_errors"] == 1
    assert recovered["source_mode"] == "LIVE"
    assert recovered["consecutive_errors"] == 0
    assert recovered["last_error"] is None


def test_b2_fallback_disabled_stays_fail_closed() -> None:
    config = RuntimeConfig(
        requested_mode="LIVE",
        fixture_id=None,
        models_explicitly_configured=False,
        replay_fallback=False,
        auto_start=False,
        txline=TxLineConfig(origin="https://example.com"),
    )
    rt = ProofGuardRuntime(config, clock=lambda: NOW)
    with pytest.raises(RuntimeError):
        asyncio.run(rt.cycle_once())
    snap = asyncio.run(rt.snapshot())
    health = asyncio.run(rt.health())
    assert snap["source_mode"] == "ERROR"
    assert health["status"] == "FAIL"


# ---------------------------------------------------------------------------
# B3 - redaction and truncation
# ---------------------------------------------------------------------------
def test_b3_safe_error_redacts_and_truncates() -> None:
    rt = ProofGuardRuntime(secret_live_config(), clock=lambda: NOW)

    repeated = rt._safe_error(RuntimeError(f"{JWT} then {TOKEN} then {JWT} again"))
    assert JWT not in repeated and TOKEN not in repeated
    assert "<redacted>" in repeated

    long = rt._safe_error(RuntimeError("A" * 800 + JWT + TOKEN))
    assert len(long) <= 500
    assert JWT not in long and TOKEN not in long

    unicode_msg = rt._safe_error(RuntimeError(f"错误 {JWT} 🚀 {TOKEN}"))
    assert JWT not in unicode_msg and TOKEN not in unicode_msg

    url_msg = rt._safe_error(RuntimeError(f"https://api.example.com/x?token={TOKEN}&jwt={JWT}"))
    assert JWT not in url_msg and TOKEN not in url_msg

    try:
        try:
            raise ValueError(f"inner {JWT}")
        except ValueError as inner:
            raise RuntimeError(f"outer {TOKEN}") from inner
    except RuntimeError as exc:
        nested = rt._safe_error(exc)
    assert TOKEN not in nested


def test_b3_provider_failure_leaks_no_secret_in_public_state() -> None:
    rt = ProofGuardRuntime(secret_live_config(), client_factory=FailingTxLineClient, clock=lambda: NOW)
    asyncio.run(rt.cycle_once())
    snapshot = asyncio.run(rt.snapshot())
    health = asyncio.run(rt.health())
    serialized = json.dumps(snapshot) + json.dumps(health)
    assert JWT not in serialized
    assert TOKEN not in serialized
    assert '"guest_jwt":' not in serialized
    assert '"api_token":' not in serialized
    assert "<redacted>" in snapshot["last_error"]


# ---------------------------------------------------------------------------
# B4 - receipt/safety invariants
# ---------------------------------------------------------------------------
def _event(**overrides) -> MarketEvent:
    base = dict(
        event_id="e1",
        fixture_id="wc-1",
        market="MATCH_RESULT",
        selection="HOME",
        market_probability=0.45,
        model_probability=0.64,
        market_probability_sum=1.0,
        stale_seconds=10.0,
        proof_ready=True,
        backwards_timestamp=False,
        observed_at=NOW,
    )
    base.update(overrides)
    return MarketEvent(**base)


def test_b4_receipt_sha_changes_when_bound_field_changes() -> None:
    a = ProofGuardAutonomousAgent().process([_event()])["records"][0]
    b = ProofGuardAutonomousAgent().process([_event(market_probability=0.44)])["records"][0]
    assert a["receipt_sha256"] != b["receipt_sha256"]


def test_b4_identical_input_and_state_is_deterministic() -> None:
    a = ProofGuardAutonomousAgent().process([_event()])["records"][0]["receipt_sha256"]
    b = ProofGuardAutonomousAgent().process([_event()])["records"][0]["receipt_sha256"]
    assert a == b


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf"), 0.0, 1.0, 1.5, -0.2])
def test_b4_non_probability_values_rejected(bad: float) -> None:
    with pytest.raises(ValueError):
        _event(model_probability=bad)


def test_b4_exposure_cap_holds_under_repeated_updates() -> None:
    agent = ProofGuardAutonomousAgent()
    for i in range(30):
        events = [
            _event(event_id=f"e{i}-{sel}", selection=sel, market_probability=0.30, model_probability=0.80)
            for sel in ("HOME", "DRAW", "AWAY")
        ]
        cycle = agent.process(events)
        assert cycle["safety"]["exposure_within_limit"] is True
        assert agent.total_exposure <= agent.maximum_total_exposure + 1e-12


# ---------------------------------------------------------------------------
# O2 - lightweight health (must not build/copy the full snapshot)
# ---------------------------------------------------------------------------
def test_o2_health_does_not_call_snapshot_even_with_many_receipts() -> None:
    async def scenario() -> dict:
        rt = ProofGuardRuntime(replay_config(max_receipts=5000), clock=lambda: NOW)
        for _ in range(60):
            await rt.cycle_once()

        calls: list[int] = []
        original = rt.snapshot

        async def spy() -> dict:
            calls.append(1)
            return await original()

        rt.snapshot = spy  # type: ignore[assignment]
        health = await rt.health()
        assert calls == []  # health must not go through snapshot()
        return health

    health = asyncio.run(scenario())
    assert health["status"] == "PASS"
    assert health["cycle_count"] == 60
    assert set(health) == {
        "status",
        "source_mode",
        "task_running",
        "last_success_at",
        "cycle_count",
        "live_ready",
        "simulation_only",
    }


def test_o2_health_values_match_snapshot_projection() -> None:
    async def scenario() -> tuple[dict, dict]:
        rt = ProofGuardRuntime(replay_config(), clock=lambda: NOW)
        for _ in range(3):
            await rt.cycle_once()
        return await rt.health(), await rt.snapshot()

    health, snap = asyncio.run(scenario())
    assert health["source_mode"] == snap["source_mode"]
    assert health["task_running"] == snap["task_running"]
    assert health["last_success_at"] == snap["last_success_at"]
    assert health["cycle_count"] == snap["cycle_count"]
    assert health["live_ready"] == snap["configuration"]["live_ready"]


def test_b4_no_enter_under_review_or_block() -> None:
    agent = ProofGuardAutonomousAgent()
    # stale beyond block threshold -> BLOCK -> REJECT (never ENTER)
    blocked = agent.process([_event(stale_seconds=10_000.0, model_probability=0.9)])["records"][0]
    assert blocked["integrity"]["decision"] == "BLOCK"
    assert blocked["action"] != "ENTER"
    # proof missing -> REVIEW -> HOLD (never ENTER)
    review = agent.process([_event(proof_ready=False, model_probability=0.9)])["records"][0]
    assert review["integrity"]["decision"] == "REVIEW"
    assert review["action"] != "ENTER"
