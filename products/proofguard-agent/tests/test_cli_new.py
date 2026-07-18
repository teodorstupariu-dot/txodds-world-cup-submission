"""Tests for the judge-facing CLI commands: simulate and verify-chain."""

from __future__ import annotations

import json

from proofguard_agent.cli import main


def _run(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_simulate_single_attack_scenario_is_rejected(capsys) -> None:
    code, payload = _run(capsys, ["simulate", "--scenario", "corrupt_block"])
    assert code == 0
    assert payload["status"] == "PASS"
    assert payload["count"] == 1
    only = payload["scenarios"][0]
    assert only["scenario"] == "corrupt_block"
    assert only["integrity"] == "BLOCK"
    assert only["action"] == "REJECT"


def test_simulate_clean_scenario_enters(capsys) -> None:
    code, payload = _run(capsys, ["simulate", "--scenario", "clean_value"])
    assert code == 0
    assert payload["scenarios"][0]["integrity"] == "PASS"
    assert payload["scenarios"][0]["action"] == "ENTER"


def test_simulate_all_scenarios_runs_every_preset(capsys) -> None:
    code, payload = _run(capsys, ["simulate"])
    assert code == 0
    assert payload["count"] == 5
    names = {item["scenario"] for item in payload["scenarios"]}
    assert names == {"clean_value", "low_edge", "stale_review", "corrupt_block", "fixture_final"}


def test_simulate_chain_output_verifies(tmp_path, capsys) -> None:
    out = tmp_path / "chain.json"
    code = main(["simulate", "--chain", "--out", str(out)])
    assert code == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["chain_verification"]["status"] == "PASS"
    assert written["count"] == 5

    # And the standalone verify-chain command re-proves the same file.
    capsys.readouterr()
    code2, verdict = _run(capsys, ["verify-chain", "--input", str(out)])
    assert code2 == 0
    assert verdict["status"] == "PASS"
    assert verdict["checked"] == 5
    assert verdict["is_genesis_anchored"] is True


def test_verify_chain_detects_tampering(tmp_path, capsys) -> None:
    out = tmp_path / "chain.json"
    assert main(["simulate", "--chain", "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    data["records"][1]["action"] = "ENTER"  # tamper
    out.write_text(json.dumps(data), encoding="utf-8")

    capsys.readouterr()
    code, verdict = _run(capsys, ["verify-chain", "--input", str(out)])
    assert code == 1
    assert verdict["status"] == "FAIL"
