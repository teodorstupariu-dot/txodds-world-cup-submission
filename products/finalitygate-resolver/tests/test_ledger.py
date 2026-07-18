from __future__ import annotations

from datetime import datetime, timezone

from finalitygate import (
    FinalityGateResolver,
    OutcomeMarket,
    ResolutionEvidence,
    build_ledger,
    verify_ledger,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)


def _decision(market_id: str, declared: str, home: int, away: int, status: str = "FINAL") -> dict:
    market = OutcomeMarket(
        market_id=market_id,
        fixture_id=f"fx-{market_id}",
        market_type="MATCH_RESULT",
        selections=("HOME", "DRAW", "AWAY"),
    )
    evidence = ResolutionEvidence(
        fixture_id=f"fx-{market_id}",
        fixture_status=status,
        home_score=home,
        away_score=away,
        declared_result=declared,
        observed_at=NOW,
        proof_status="VALID",
        root_status="MATCH",
        proof_reference="p",
        expected_root="cc" * 32,
        observed_root="cc" * 32,
    )
    return FinalityGateResolver().resolve(market, evidence).to_dict()


def _batch() -> list[dict]:
    return [
        _decision("m1", "HOME", 2, 1),
        _decision("m2", "AWAY", 0, 3),
        _decision("m3", "DRAW", 1, 1),
    ]


def test_ledger_links_and_verifies() -> None:
    ledger = build_ledger(_batch())
    assert ledger["count"] == 3
    assert ledger["entries"][0]["prev_entry_hash"] == ledger["genesis"]
    for parent, child in zip(ledger["entries"], ledger["entries"][1:]):
        assert child["prev_entry_hash"] == parent["entry_hash"]
    assert len(ledger["batch_root"]) == 64
    verdict = verify_ledger(ledger)
    assert verdict["status"] == "PASS"
    assert verdict["checked"] == 3


def test_ledger_detects_reorder() -> None:
    ledger = build_ledger(_batch())
    ledger["entries"] = [ledger["entries"][1], ledger["entries"][0], ledger["entries"][2]]
    assert verify_ledger(ledger)["status"] == "FAIL"


def test_ledger_detects_tampered_entry() -> None:
    ledger = build_ledger(_batch())
    ledger["entries"][0]["state"] = "DISPUTE"
    assert verify_ledger(ledger)["status"] == "FAIL"


def test_ledger_detects_batch_root_tamper() -> None:
    ledger = build_ledger(_batch())
    ledger["batch_root"] = "ab" * 32
    assert verify_ledger(ledger)["status"] == "FAIL"


def test_ledger_is_deterministic() -> None:
    assert build_ledger(_batch())["batch_root"] == build_ledger(_batch())["batch_root"]
