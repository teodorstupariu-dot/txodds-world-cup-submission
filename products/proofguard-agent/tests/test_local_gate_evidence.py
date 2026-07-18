from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_local_gate_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "local_gate.py"
    spec = importlib.util.spec_from_file_location("proofguard_local_gate", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_step_passed_requires_explicit_named_pass() -> None:
    local_gate = _load_local_gate_module()

    assert local_gate._step_passed([], "wheel-files") is False
    assert local_gate._step_passed(
        [{"name": "wheel-files", "status": "FAIL"}],
        "wheel-files",
    ) is False
    assert local_gate._step_passed(
        [{"name": "other-step", "status": "PASS"}],
        "wheel-files",
    ) is False
    assert local_gate._step_passed(
        [
            {"name": "wheel-files", "status": "FAIL"},
            {"name": "wheel-files", "status": "PASS"},
        ],
        "wheel-files",
    ) is True
