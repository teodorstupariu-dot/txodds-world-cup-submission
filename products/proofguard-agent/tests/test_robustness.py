"""Seeded property-style robustness tests for the autonomous agent.

No third-party property framework (e.g. hypothesis) is available in the locked
runtime, so these use deterministic seeded loops over a wide random input space.
They assert the safety-critical invariants that must hold for EVERY input:

  1. Safety     — the agent never ENTERs a position on a non-PASS integrity gate.
  2. Exposure   — total exposure never exceeds the configured cap.
  3. Integrity  — every emitted receipt re-hashes to its own SHA-256.
  4. Chain      — the full receipt history verifies as an append-only chain.

A single counter-example fails the build, which is the point.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from proofguard_agent import (
    GENESIS_RECEIPT,
    MarketEvent,
    ProofGuardAutonomousAgent,
    verify_receipt,
    verify_receipt_chain,
)

BASE = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)
SELECTIONS = ("HOME", "DRAW", "AWAY")


def _random_event(rng: random.Random, index: int) -> MarketEvent:
    """A deliberately adversarial random market snapshot."""
    probability_sum = rng.choice([1.0, 1.0, 1.0, rng.uniform(0.6, 1.4)])
    return MarketEvent(
        event_id=f"fuzz-{index}",
        fixture_id=f"fixture-{index % 5}",
        market="MATCH_RESULT",
        selection=rng.choice(SELECTIONS),
        market_probability=rng.uniform(0.02, 0.98),
        model_probability=rng.uniform(0.02, 0.98),
        market_probability_sum=probability_sum,
        stale_seconds=rng.choice([5.0, 30.0, 95.0, rng.uniform(0.0, 600.0)]),
        proof_ready=rng.random() > 0.25,
        backwards_timestamp=rng.random() > 0.85,
        observed_at=BASE + timedelta(seconds=index),
        fixture_final=rng.random() > 0.9,
        winning_selection=rng.choice(SELECTIONS) if rng.random() > 0.5 else None,
    )


def test_safety_and_chain_invariants_hold_over_random_stream() -> None:
    for seed in range(60):
        rng = random.Random(seed)
        agent = ProofGuardAutonomousAgent()
        history: list[dict] = []
        for cycle_index in range(rng.randint(1, 8)):
            batch = [_random_event(rng, cycle_index * 10 + i) for i in range(rng.randint(1, 5))]
            cycle = agent.process(batch)
            for record in cycle["records"]:
                # 1. Safety: never ENTER when the gate did not PASS.
                if record["action"] == "ENTER":
                    assert record["integrity"]["decision"] == "PASS", (seed, record)
                # 3. Integrity: each receipt re-hashes to itself.
                assert verify_receipt(record)["status"] == "PASS", (seed, record)
                history.append(record)
            # 2. Exposure never exceeds the cap.
            assert agent.total_exposure <= agent.maximum_total_exposure + 1e-9, seed

        # 4. The whole append-only history verifies end-to-end.
        verdict = verify_receipt_chain(history, genesis=GENESIS_RECEIPT)
        assert verdict["status"] == "PASS", (seed, verdict["errors"])
        assert verdict["checked"] == len(history)


def test_tampering_with_any_past_receipt_is_detected() -> None:
    rng = random.Random(1234)
    agent = ProofGuardAutonomousAgent()
    history: list[dict] = []
    for i in range(6):
        history.extend(agent.process([_random_event(rng, i)])["records"])
    assert verify_receipt_chain(history)["status"] == "PASS"

    # Flip one field deep in the chain; the chain must now fail.
    tampered = [dict(r) for r in history]
    tampered[2] = {**tampered[2], "action": "ENTER"}
    assert verify_receipt_chain(tampered)["status"] == "FAIL"

    # Reordering must also break linkage even if each receipt is individually valid.
    reordered = list(history)
    reordered[1], reordered[3] = reordered[3], reordered[1]
    assert verify_receipt_chain(reordered)["status"] == "FAIL"
