from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "exports"

PROJECTS = {
    "proofguard": {
        "template": ROOT / "products" / "proofguard-agent" / "SUBMISSION.md",
        "local_report": ROOT / "products" / "proofguard-agent" / "outputs" / "local_validation_report.json",
        "release_report": ROOT / "products" / "proofguard-agent" / "RELEASE_REPORT.json",
        "output": EXPORTS / "proofguard-agent-SUBMISSION_FINAL.md",
        "placeholders": {
            "PENDING_TEST_COUNT": "tests",
            "PENDING_DEMO_SHA256": "deterministic_demo_sha256",
            "PENDING_VALIDATION_FINGERPRINT": "odds_validation_structural_fingerprint_sha256",
            "PENDING_JUDGE_PACK": "judge_pack",
            "PENDING_JUDGE_PACK_SHA256": "judge_pack_sha256",
        },
        "text_placeholders": {
            "PENDING_TXODDS_POSITIVE_FEEDBACK": "txodds_positive_feedback",
            "PENDING_TXODDS_FRICTION": "txodds_friction",
            "PENDING_TXODDS_SUGGESTION": "txodds_suggestion",
        },
    },
    "finalitygate": {
        "template": ROOT / "products" / "finalitygate-resolver" / "SUBMISSION.md",
        "local_report": ROOT / "products" / "finalitygate-resolver" / "outputs" / "local_validation_report.json",
        "release_report": ROOT / "products" / "finalitygate-resolver" / "RELEASE_REPORT.json",
        "output": EXPORTS / "finalitygate-resolver-SUBMISSION_FINAL.md",
        "placeholders": {
            "PENDING_TEST_COUNT": "tests",
            "PENDING_DEMO_SHA256": "deterministic_demo_sha256",
            "PENDING_VALIDATION_FINGERPRINT": "validation_structural_fingerprint_sha256",
            "PENDING_JUDGE_PACK": "judge_pack",
            "PENDING_JUDGE_PACK_SHA256": "judge_pack_sha256",
        },
        "text_placeholders": {},
    },
}

LINK_PLACEHOLDERS = {
    "PENDING_PUBLIC_REPOSITORY": "public_repository",
    "PENDING_PUBLIC_DEMO": "public_demo",
    "PENDING_VIDEO_URL": "video",
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read valid JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or len(value) != 40:
        raise ValueError(f"unable to resolve git HEAD: {completed.stderr.strip()}")
    return value


def _require_https(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty HTTPS URL")
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"{field} must be a credential-free HTTPS URL")
    if "example" in parsed.netloc.lower() or "owner" in url.lower() or "pending" in url.lower():
        raise ValueError(f"{field} still appears to be a placeholder")
    return url


def _require_feedback(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text based on the actual TxLINE integration")
    text = " ".join(value.strip().split())
    if len(text) < 20:
        raise ValueError(f"{field} must contain at least 20 meaningful characters")
    lowered = text.lower()
    if any(marker in lowered for marker in ("pending", "todo", "example feedback", "replace me")):
        raise ValueError(f"{field} still appears to be placeholder text")
    return text


def _validate_release(project: str, config: dict[str, Any], current_commit: str) -> tuple[dict[str, Any], dict[str, Any]]:
    local_report = _read_json(Path(config["local_report"]))
    release_report = _read_json(Path(config["release_report"]))
    if local_report.get("status") != "PASS":
        raise ValueError(f"{project}: local validation report is not PASS")
    if release_report.get("status") != "PASS":
        raise ValueError(f"{project}: release report is not PASS")
    report_commit = local_report.get("commit")
    if report_commit != current_commit:
        raise ValueError(f"{project}: local report commit {report_commit!r} does not match HEAD {current_commit}")
    if int(release_report.get("tests", 0) or 0) <= 0:
        raise ValueError(f"{project}: release report has no positive test count")
    for field in ("deterministic_demo_sha256", "judge_pack_sha256"):
        value = release_report.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError(f"{project}: invalid {field}")
    fingerprint_field = (
        "odds_validation_structural_fingerprint_sha256"
        if project == "proofguard"
        else "validation_structural_fingerprint_sha256"
    )
    fingerprint = release_report.get(fingerprint_field)
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise ValueError(f"{project}: invalid {fingerprint_field}")
    if not isinstance(release_report.get("judge_pack"), str) or not release_report["judge_pack"]:
        raise ValueError(f"{project}: judge pack path missing")
    if project == "proofguard":
        required_live_checks = {
            "web_app_smoke": release_report.get("web_app_smoke"),
            "web_app_isolated_wheel_smoke": release_report.get("web_app_isolated_wheel_smoke"),
            "live_web_packaged": release_report.get("live_web_packaged"),
        }
        failed = [name for name, passed in required_live_checks.items() if passed is not True]
        if failed:
            raise ValueError(f"proofguard: live web release checks are not PASS: {failed}")
        endpoints = release_report.get("web_endpoints")
        if not isinstance(endpoints, list) or "/api/health" not in endpoints or "/api/snapshot" not in endpoints:
            raise ValueError("proofguard: required public web endpoints missing from release evidence")
    return local_report, release_report


def _render_packet(
    project: str,
    config: dict[str, Any],
    release_report: dict[str, Any],
    links: dict[str, Any],
    commit: str,
) -> tuple[str, dict[str, Any]]:
    template_path = Path(config["template"])
    try:
        text = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"unable to read {template_path}: {exc}") from exc

    replacements: dict[str, str] = {"PENDING_FINAL_COMMIT": commit}
    for placeholder, release_field in config["placeholders"].items():
        value = release_report.get(release_field)
        if value is None or value == "":
            raise ValueError(f"{project}: release field {release_field} missing")
        replacements[placeholder] = str(value)

    for placeholder, link_field in LINK_PLACEHOLDERS.items():
        replacements[placeholder] = _require_https(links.get(link_field), f"{project}.{link_field}")

    for placeholder, text_field in config.get("text_placeholders", {}).items():
        replacements[placeholder] = _require_feedback(links.get(text_field), f"{project}.{text_field}")

    for placeholder in replacements:
        if placeholder not in text:
            raise ValueError(f"{project}: expected placeholder {placeholder} is missing from template")

    # Substitute every managed placeholder in a single pass. Sorting the
    # alternation longest-first means an overlapping pair such as
    # PENDING_JUDGE_PACK and PENDING_JUDGE_PACK_SHA256 can never corrupt each
    # other, and a single pass guarantees a substituted value is never rescanned
    # as if it were itself a placeholder.
    pattern = re.compile("|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)))
    text = pattern.sub(lambda match: replacements[match.group(0)], text)

    # Only the placeholders this finalizer manages must be fully resolved.
    # Legitimate domain tokens that merely share the PENDING_ prefix (for
    # example the FinalityGate resolver state PENDING_FINALITY) are content, not
    # finalizer placeholders, and must not trip this guard.
    unresolved = sorted(token for token in replacements if token in text)
    if unresolved:
        raise ValueError(f"{project}: unresolved placeholders remain: {unresolved}")

    evidence = {
        "project": project,
        "commit": commit,
        "tests": release_report["tests"],
        "deterministic_demo_sha256": release_report["deterministic_demo_sha256"],
        "judge_pack": release_report["judge_pack"],
        "judge_pack_sha256": release_report["judge_pack_sha256"],
        "public_repository": replacements["PENDING_PUBLIC_REPOSITORY"],
        "public_demo": replacements["PENDING_PUBLIC_DEMO"],
        "video": replacements["PENDING_VIDEO_URL"],
    }
    if project == "proofguard":
        evidence["web_app_smoke"] = release_report["web_app_smoke"]
        evidence["web_app_isolated_wheel_smoke"] = release_report["web_app_isolated_wheel_smoke"]
        evidence["web_endpoints"] = release_report["web_endpoints"]
        evidence["txodds_feedback"] = {
            field: links[field]
            for field in (
                "txodds_positive_feedback",
                "txodds_friction",
                "txodds_suggestion",
            )
        }
    return text, evidence


def _selected_projects(values: list[str] | None) -> list[str]:
    """Return a deterministic, duplicate-free project selection."""

    if not values:
        return list(PROJECTS)
    return list(dict.fromkeys(values))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate final submission Markdown from exact local PASS evidence")
    parser.add_argument("--links", type=Path, default=ROOT / "submission_links.json")
    parser.add_argument(
        "--project",
        dest="projects",
        action="append",
        choices=sorted(PROJECTS),
        help="Finalize only the selected project. Repeat to select multiple projects. Defaults to all projects.",
    )
    args = parser.parse_args(argv)
    selected_projects = _selected_projects(args.projects)

    report: dict[str, Any] = {
        "schema": "worldcup.final-submission-packets.v2",
        "status": "FAIL",
        "selected_projects": selected_projects,
        "projects": {},
        "errors": [],
    }
    try:
        links_payload = _read_json(args.links)
        commit = _git_commit()
        EXPORTS.mkdir(parents=True, exist_ok=True)
        for project in selected_projects:
            config = PROJECTS[project]
            _, release_report = _validate_release(project, config, commit)
            project_links = links_payload.get(project)
            if not isinstance(project_links, dict):
                raise ValueError(f"links file is missing object for {project}")
            text, evidence = _render_packet(project, config, release_report, project_links, commit)
            output = Path(config["output"])
            output.write_text(text, encoding="utf-8")
            try:
                output_display = output.relative_to(ROOT).as_posix()
            except ValueError:
                # Output configured outside the repository root (e.g. a test
                # temp dir): report the absolute path rather than failing.
                output_display = output.as_posix()
            report["projects"][project] = {
                "status": "PASS",
                "output": output_display,
                "evidence": evidence,
            }
        report["status"] = "PASS"
    except ValueError as exc:
        report["errors"].append(str(exc))

    output_report = EXPORTS / "FINAL_SUBMISSION_REPORT.json"
    output_report.parent.mkdir(parents=True, exist_ok=True)
    output_report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
