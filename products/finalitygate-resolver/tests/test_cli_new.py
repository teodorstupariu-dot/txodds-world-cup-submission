"""Tests for the judge-facing CLI commands: commitment, explain, ledger."""

from __future__ import annotations

import json

from finalitygate.cli import main

MARKET = {
    "market_id": "wc-final-1x2",
    "fixture_id": "wc-final-001",
    "market_type": "MATCH_RESULT",
    "selections": ["HOME", "DRAW", "AWAY"],
    "policy_version": "finalitygate-v1",
}


def _evidence(**overrides) -> dict:
    base = {
        "fixture_id": "wc-final-001",
        "fixture_status": "FINAL",
        "home_score": 2,
        "away_score": 1,
        "declared_result": "HOME",
        "observed_at": "2026-07-13T20:00:00+00:00",
        "proof_status": "VALID",
        "root_status": "MATCH",
        "proof_reference": "proof-abc",
        "expected_root": "ab" * 32,
        "observed_root": "ab" * 32,
    }
    base.update(overrides)
    return base


def _write(tmp_path, name, payload) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_commitment_resolves_and_self_verifies(tmp_path) -> None:
    market = _write(tmp_path, "market.json", MARKET)
    evidence = _write(tmp_path, "evidence.json", _evidence())
    out = tmp_path / "commit.json"
    code = main(["commitment", "--market", market, "--evidence", evidence, "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["state"] == "RESOLVE"
    assert len(payload["root"]) == 64
    assert payload["inclusion_proof_self_check"]["verified"] is True


def test_commitment_disputes_on_score_conflict(tmp_path) -> None:
    market = _write(tmp_path, "market.json", MARKET)
    # declared HOME but score says AWAY won -> must not resolve.
    evidence = _write(tmp_path, "evidence.json", _evidence(home_score=0, away_score=3))
    out = tmp_path / "commit.json"
    code = main(["commitment", "--market", market, "--evidence", evidence, "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["state"] == "DISPUTE"
    # The commitment still builds and self-verifies even on a disputed decision.
    assert payload["inclusion_proof_self_check"]["verified"] is True


def test_explain_lists_checks_and_remediation(tmp_path) -> None:
    market = _write(tmp_path, "market.json", MARKET)
    evidence = _write(tmp_path, "evidence.json", _evidence(proof_status="MISSING"))
    out = tmp_path / "explain.json"
    code = main(["explain", "--market", market, "--evidence", evidence, "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["state"] != "RESOLVE"
    assert "checks_passed" in payload
    assert isinstance(payload["remediation"], list)


def test_ledger_builds_and_verifies(tmp_path, capsys) -> None:
    out = tmp_path / "ledger.json"
    code = main(["ledger", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["verification"]["status"] == "PASS"
    assert payload["ledger"]["count"] >= 1
    assert len(payload["ledger"]["batch_root"]) == 64
