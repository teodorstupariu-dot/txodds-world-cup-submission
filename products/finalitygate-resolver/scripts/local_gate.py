from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "outputs" / "local_validation_report.json"
RELEASE_REPORT = ROOT / "RELEASE_REPORT.json"
TEST_COLLECTION = ROOT / "outputs" / "test_collection.json"
RELEASE_ASSETS = ROOT / "outputs" / "release_assets.json"
VALIDATION_FIXTURE = ROOT / "tests" / "fixtures" / "valid_score_validation.json"
MAX_CAPTURE_CHARS = 20_000


def _tail(value: str | None) -> str:
    text = value or ""
    if len(text) <= MAX_CAPTURE_CHARS:
        return text
    return f"<truncated {len(text) - MAX_CAPTURE_CHARS} chars>\n" + text[-MAX_CAPTURE_CHARS:]


def _run(name: str, argv: list[str], *, cwd: Path = ROOT, timeout: int = 900, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, shell=False, check=False)
        return {"name": name, "status": "PASS" if completed.returncode == 0 else "FAIL", "returncode": completed.returncode, "duration_seconds": round(time.perf_counter() - started, 3), "command": argv, "stdout_tail": _tail(completed.stdout), "stderr_tail": _tail(completed.stderr)}
    except subprocess.TimeoutExpired as exc:
        return {"name": name, "status": "TIMEOUT", "returncode": None, "duration_seconds": round(time.perf_counter() - started, 3), "command": argv, "stdout_tail": _tail(exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout), "stderr_tail": _tail(exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr)}
    except OSError as exc:
        return {"name": name, "status": "ERROR", "returncode": None, "duration_seconds": round(time.perf_counter() - started, 3), "command": argv, "stdout_tail": "", "stderr_tail": f"{type(exc).__name__}: {exc}"}


def _digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8")); digest.update(b"\0"); digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()


def _git_commit() -> str | None:
    result = _run("git-commit", ["git", "rev-parse", "HEAD"], timeout=10)
    value = result["stdout_tail"].strip()
    return value if result["status"] == "PASS" and len(value) == 40 else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _contract(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "returncode": 0 if passed else 1, "duration_seconds": 0.0, "command": ["internal-contract-check"], "stdout_tail": detail if passed else "", "stderr_tail": "" if passed else detail}


def _step_passed(steps: list[dict[str, Any]], name: str) -> bool:
    """Require positive evidence from an explicit named PASS step."""

    return any(step.get("name") == name and step.get("status") == "PASS" for step in steps)


def run_gate(timeout: int) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []

    def execute(name: str, argv: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> bool:
        print(f"[finalitygate-local] {name}", flush=True)
        result = _run(name, argv, cwd=cwd, timeout=timeout, env=env)
        steps.append(result)
        return result["status"] == "PASS"

    dist = ROOT / "dist"
    if dist.exists():
        shutil.rmtree(dist)

    with tempfile.TemporaryDirectory(prefix="finalitygate-release-") as temp_name:
        temp = Path(temp_name)
        demo_a = temp / "demo-a"
        demo_b = temp / "demo-b"
        validation_inspection = temp / "validation-inspection.json"
        commands = [
            ("compile", [sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts"]),
            ("collect-tests", [sys.executable, "scripts/count_tests.py"]),
            ("pytest", [sys.executable, "-m", "pytest", "-q"]),
            ("doctor", [sys.executable, "-m", "finalitygate", "doctor"]),
            ("web-smoke", [sys.executable, "-m", "finalitygate.web.smoke"]),
            (
                "inspect-official-validation-fixture",
                [
                    sys.executable,
                    "-m",
                    "finalitygate",
                    "inspect-score-validation",
                    "--input",
                    str(VALIDATION_FIXTURE),
                    "--out",
                    str(validation_inspection),
                ],
            ),
            ("demo-a", [sys.executable, "-m", "finalitygate", "demo", "--out", str(demo_a)]),
            ("verify-demo-a", [sys.executable, "-m", "finalitygate", "verify-demo", str(demo_a)]),
            ("demo-b", [sys.executable, "-m", "finalitygate", "demo", "--out", str(demo_b)]),
            ("verify-demo-b", [sys.executable, "-m", "finalitygate", "verify-demo", str(demo_b)]),
        ]
        for name, argv in commands:
            if not execute(name, argv):
                break

        deterministic_hash: str | None = None
        if len(steps) == len(commands) and all(step["status"] == "PASS" for step in steps):
            hash_a = _digest_tree(demo_a); hash_b = _digest_tree(demo_b); deterministic_hash = hash_a
            steps.append(_contract("deterministic-demo", hash_a == hash_b, f"{hash_a} == {hash_b}"))

        if all(step["status"] == "PASS" for step in steps):
            execute("package-build", [sys.executable, "-m", "build", "--no-isolation"])

        wheels = sorted(dist.glob("*.whl")); sdists = sorted(dist.glob("*.tar.gz"))
        package_ok = len(wheels) == 1 and len(sdists) == 1
        if all(step["status"] == "PASS" for step in steps):
            steps.append(_contract("package-contract", package_ok, f"wheels={wheels}; sdists={sdists}"))

        if package_ok and all(step["status"] == "PASS" for step in steps):
            wheel_target = temp / "wheel-install"; wheel_target.mkdir()
            with zipfile.ZipFile(wheels[0]) as archive:
                archive.extractall(wheel_target)
            required = {
                "finalitygate/core.py",
                "finalitygate/cli.py",
                "finalitygate/demo.py",
                "finalitygate/txline.py",
                "finalitygate/validation.py",
                "finalitygate/web/__init__.py",
                "finalitygate/web/app.py",
                "finalitygate/web/smoke.py",
                "finalitygate/web/__main__.py",
            }
            extracted = {path.relative_to(wheel_target).as_posix() for path in wheel_target.rglob("*") if path.is_file()}
            missing = required - extracted
            steps.append(_contract("wheel-files", not missing, f"missing={sorted(missing)}"))
            if not missing:
                wheel_demo = temp / "wheel-demo"
                wheel_validation = temp / "wheel-validation.json"
                clean_env = os.environ.copy(); clean_env["PYTHONPATH"] = str(wheel_target); clean_env["PYTHONNOUSERSITE"] = "1"
                execute("isolated-wheel-doctor", [sys.executable, "-m", "finalitygate", "doctor"], cwd=temp, env=clean_env)
                execute("isolated-wheel-web-smoke", [sys.executable, "-m", "finalitygate.web.smoke"], cwd=temp, env=clean_env)
                execute(
                    "isolated-wheel-validation-inspection",
                    [
                        sys.executable,
                        "-m",
                        "finalitygate",
                        "inspect-score-validation",
                        "--input",
                        str(VALIDATION_FIXTURE),
                        "--out",
                        str(wheel_validation),
                    ],
                    cwd=temp,
                    env=clean_env,
                )
                execute("isolated-wheel-demo", [sys.executable, "-m", "finalitygate", "demo", "--out", str(wheel_demo)], cwd=temp, env=clean_env)
                execute("isolated-wheel-verify", [sys.executable, "-m", "finalitygate", "verify-demo", str(wheel_demo)], cwd=temp, env=clean_env)

        if all(step["status"] == "PASS" for step in steps):
            execute("security-sbom-judge-pack", [sys.executable, "scripts/release_assets.py", "--demo-root", str(demo_a)])

        summary_a = _read_json(demo_a / "summary.json")
        states = set((summary_a or {}).get("state_counts", {}))
        required_states = {"OPEN", "PENDING_FINALITY", "WAIT_FOR_PROOF", "RESOLVE", "DISPUTE"}
        validation_result = _read_json(validation_inspection) or {}
        collection = _read_json(TEST_COLLECTION) or {}
        tests_collected = int(collection.get("tests_collected", 0) or 0)
        release_assets = _read_json(RELEASE_ASSETS) or {}
        if all(step["status"] == "PASS" for step in steps):
            steps.append(_contract("test-count-contract", tests_collected > 0, f"tests_collected={tests_collected}"))
            steps.append(_contract("state-coverage", required_states.issubset(states), f"states={sorted(states)}"))
            steps.append(_contract("official-validation-structure", validation_result.get("status") == "PASS" and validation_result.get("onchain_view_executed") is False, f"status={validation_result.get('status')}; onchain_view_executed={validation_result.get('onchain_view_executed')}"))
            steps.append(_contract("release-assets-contract", release_assets.get("status") == "PASS", f"status={release_assets.get('status')}"))

        status = "PASS" if steps and all(step["status"] == "PASS" for step in steps) else "FAIL"
        judge_pack = release_assets.get("judge_pack") if isinstance(release_assets.get("judge_pack"), dict) else {}
        sbom = release_assets.get("sbom") if isinstance(release_assets.get("sbom"), dict) else {}
        security = release_assets.get("security") if isinstance(release_assets.get("security"), dict) else {}
        # Positive booleans require an explicit named PASS step; the absence of a
        # FAIL is never treated as success.
        web_smoke_passed = _step_passed(steps, "web-smoke")
        isolated_web_smoke_passed = _step_passed(steps, "isolated-wheel-web-smoke")
        wheel_files_passed = _step_passed(steps, "wheel-files")
        release = {
            "status": status,
            "version": "0.1.0",
            "python": sys.version,
            "tests": tests_collected,
            "deterministic_demo_sha256": deterministic_hash,
            "state_coverage": sorted(states),
            "web_app_smoke": web_smoke_passed,
            "web_app_isolated_wheel_smoke": isolated_web_smoke_passed,
            "web_app_packaged": wheel_files_passed,
            "web_endpoints": [
                "/",
                "/api/health",
                "/api/status",
                "/api/demo",
                "/api/resolve",
                "/api/verify-receipt",
                "/api/docs",
                "/api/openapi.json",
            ],
            "official_validation_structure_checked": validation_result.get("status") == "PASS",
            "validation_structural_fingerprint_sha256": validation_result.get("structural_fingerprint_sha256"),
            "onchain_view_executed": False,
            "wheel": wheels[0].name if len(wheels) == 1 else None,
            "sdist": sdists[0].name if len(sdists) == 1 else None,
            "wheel_isolated_execution": _step_passed(steps, "isolated-wheel-verify"),
            "security_scan_status": security.get("status"),
            "sbom": sbom.get("path"),
            "sbom_sha256": sbom.get("sha256"),
            "judge_pack": judge_pack.get("path"),
            "judge_pack_sha256": judge_pack.get("sha256"),
            "claim_boundary": "official TxODDS score-validation payload structure and PDA seed inputs are checked; complete proof validation still requires the Solana validateStat view call against the corresponding on-chain account",
        }
        RELEASE_REPORT.write_text(json.dumps(release, indent=2) + "\n", encoding="utf-8")

        if status == "PASS":
            final_demo = ROOT / "outputs" / "demo"
            if final_demo.exists():
                shutil.rmtree(final_demo)
            shutil.copytree(demo_a, final_demo)

    return {
        "schema": "finalitygate.local-validation-report.v1",
        "status": "PASS" if steps and all(step["status"] == "PASS" for step in steps) else "FAIL",
        "commit": _git_commit(),
        "python": sys.version,
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "steps": steps,
        "release_report": str(RELEASE_REPORT),
        "test_collection": str(TEST_COLLECTION),
        "release_assets": str(RELEASE_ASSETS),
        "artifacts": {"demo": str(ROOT / "outputs" / "demo"), "dist": str(ROOT / "dist")},
        "notes": [
            "Runs locally only and does not call GitHub Actions.",
            "Does not read or record wallet secrets, JWTs, API tokens, or .env values.",
            "Offline official-payload inspection is not equivalent to executing the Solana validateStat view method.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run FinalityGate validation locally without GitHub Actions")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args(argv)
    report = run_gate(args.timeout)
    report_path = args.report if args.report.is_absolute() else ROOT / args.report
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path.resolve()), "commit": report["commit"]}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
