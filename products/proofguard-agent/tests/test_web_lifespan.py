from __future__ import annotations

import time

from fastapi.testclient import TestClient

from proofguard_agent.txline import TxLineConfig
from proofguard_agent.web.app import create_app
from proofguard_agent.web.runtime import ProofGuardRuntime, RuntimeConfig


def test_lifespan_autostart_runs_immediately_and_repeats() -> None:
    """The deployed FastAPI lifespan must drive repeated autonomous cycles.

    This protects the production path rather than only exercising manual
    ``cycle_once`` calls. The interval respects the same lower bound accepted
    by ``RuntimeConfig.from_env``.
    """

    config = RuntimeConfig(
        requested_mode="REPLAY",
        auto_start=True,
        poll_seconds=0.25,
        max_receipts=20,
        txline=TxLineConfig(origin="https://example.com"),
    )
    runtime = ProofGuardRuntime(config)
    app = create_app(runtime=runtime)

    with TestClient(app) as client:
        deadline = time.monotonic() + 2.0
        snapshot = client.get("/api/snapshot").json()
        while snapshot["cycle_count"] < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
            snapshot = client.get("/api/snapshot").json()

        assert snapshot["task_running"] is True
        assert snapshot["cycle_count"] >= 2
        assert snapshot["source_mode"] == "REPLAY"
        assert snapshot["last_success_at"] is not None
        assert snapshot["latest_decision"] is not None
        assert snapshot["latest_cycle"]["safety"]["unsafe_entry_count"] == 0

    assert runtime.task_running is False
