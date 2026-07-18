"""Seeded property-style robustness tests for the finality resolver.

No third-party property framework is available in the locked runtime, so these
use deterministic seeded loops over a wide random evidence space. They assert
the fail-closed invariants that must hold for EVERY input:

  1. Fail-closed  — a market only RESOLVEs when finality, score, declared result,
                    proof, and canonical roots all agree; any conflict disputes.
  2. Integrity    — every resolution receipt re-hashes to its own SHA-256.
  3. Commitment   — every leaf's Merkle inclusion proof folds to the committed
                    root (what a settlement contract would check on-chain).
  4. Ledger       — a batch of resolutions always builds and verifies.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from finalitygate import (
    FinalityGateResolver,
    OutcomeMarket,
    ResolutionEvidence,
    build_commitment,
    build_ledger,
    verify_ledger,
    verify_proof,
    verify_receipt,
)

BASE = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
SELECTIONS = ("HOME", "DRAW", "AWAY")
GOOD_ROOT = "cc" * 32


def _derive(home: int | None, away: int | None) -> str | None:
    if home is None or away is None:
        return None
    if home > away:
        return "HOME"
    if away > home:
        return "AWAY"
    return "DRAW"


def _random_case(rng: random.Random, index: int):
    market = OutcomeMarket(
        market_id=f"m-{index}",
        fixture_id=f"fx-{index}",
        market_type="MATCH_RESULT",
        selections=SELECTIONS,
    )
    home = rng.choice([None, 0, 1, 2, 3])
    away = rng.choice([None, 0, 1, 2, 3])
    # Adversarial roots: sometimes canonical, sometimes junk, sometimes disagreeing.
    root_choice = rng.random()
    if root_choice < 0.5:
        expected = observed = GOOD_ROOT
    elif root_choice < 0.75:
        expected, observed = GOOD_ROOT, "dd" * 32
    else:
        expected = observed = rng.choice(["aa", "", None])
    evidence = ResolutionEvidence(
        fixture_id=f"fx-{index}",
        fixture_status=rng.choice(["FINAL", "FINAL", "LIVE", "SCHEDULED"]),
        home_score=home,
        away_score=away,
        declared_result=rng.choice([*SELECTIONS, None, "HOME"]),
        observed_at=BASE + timedelta(seconds=index),
        proof_status=rng.choice(["VALID", "VALID", "MISSING", "INVALID", "UNVERIFIED"]),
        root_status=rng.choice(["MATCH", "MATCH", "MISSING", "MISMATCH", "UNVERIFIED"]),
        proof_reference=rng.choice(["p", None]),
        expected_root=expected,
        observed_root=observed,
    )
    return market, evidence


def test_fail_closed_and_commitment_invariants_over_random_evidence() -> None:
    resolver = FinalityGateResolver()
    for seed in range(120):
        rng = random.Random(seed)
        market, evidence = _random_case(rng, seed)
        decision = resolver.resolve(market, evidence).to_dict()

        # 1. Fail-closed: RESOLVE only when everything genuinely agrees.
        if decision["state"] == "RESOLVE":
            derived = _derive(evidence.home_score, evidence.away_score)
            assert evidence.fixture_status == "FINAL", (seed, decision)
            assert derived is not None and evidence.declared_result == derived, (seed, decision)
            assert evidence.declared_result in market.selections, (seed, decision)
            assert evidence.proof_status == "VALID", (seed, decision)
            assert evidence.root_status == "MATCH", (seed, decision)
            assert decision["resolved_selection"] == evidence.declared_result

        # A declared result that contradicts the score can NEVER resolve.
        derived = _derive(evidence.home_score, evidence.away_score)
        if derived is not None and evidence.declared_result not in (None, derived):
            assert decision["state"] != "RESOLVE", (seed, decision)

        # 2. Integrity: the receipt re-hashes to itself.
        assert verify_receipt(decision)["status"] == "PASS", seed

        # 3. Commitment: every leaf's inclusion proof folds to the root.
        commit = build_commitment(decision)
        for leaf in commit["leaves"]:
            assert verify_proof(leaf["leaf_hash"], commit["proofs"][leaf["field"]], commit["root"]), (seed, leaf["field"])


def test_random_batches_build_and_verify_as_a_ledger() -> None:
    resolver = FinalityGateResolver()
    for seed in range(40):
        rng = random.Random(1000 + seed)
        decisions = [resolver.resolve(*_random_case(rng, seed * 10 + i)).to_dict() for i in range(rng.randint(1, 6))]
        ledger = build_ledger(decisions)
        assert ledger["count"] == len(decisions)
        assert verify_ledger(ledger)["status"] == "PASS", seed

        # Tampering with one entry's committed body must break the hash chain.
        if len(ledger["entries"]) >= 2:
            broken = {**ledger, "entries": [dict(e) for e in ledger["entries"]]}
            broken["entries"][1] = {**broken["entries"][1], "commitment_root": "0" * 64}
            assert verify_ledger(broken)["status"] == "FAIL", seed
