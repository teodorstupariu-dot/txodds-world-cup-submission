from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
OUTPUT = ROOT / "outputs" / "release_assets.json"
SBOM = DIST / "proofguard-autonomous-agent.spdx.json"
JUDGE_PACK = DIST / "proofguard-autonomous-agent-judge-pack.zip"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)

EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build", "outputs"}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".yaml", ".yml", ".txt", ".ps1", ".sh", ".html", ".example", ".gitignore", ".dockerignore"}
FORBIDDEN_FILE_NAMES = {".env", "id_rsa", "id_ed25519"}
SECRET_PATTERNS = {
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "seed_phrase_assignment": re.compile(r"(?i)\b(?:seed[_ -]?phrase|mnemonic)\s*[:=]\s*['\"][^'\"]{16,}['\"]"),
    "bearer_literal": re.compile(r"(?i)authorization\s*[:=]\s*['\"]bearer\s+[A-Za-z0-9._~+/=-]{20,}['\"]"),
}

FileEntry = tuple[Path, str]

STANDALONE_METADATA = (
    ROOT / ".dockerignore",
    ROOT / ".env.example",
    ROOT / ".gitignore",
    ROOT / "Dockerfile",
    ROOT / "LICENSE",
    ROOT / "README.md",
    ROOT / "SUBMISSION.md",
    ROOT / "pyproject.toml",
    ROOT / "render.yaml",
    ROOT / "railway.json",
)


def _source_entries(demo_root: Path) -> list[FileEntry]:
    roots = [*STANDALONE_METADATA, ROOT / "src", ROOT / "tests", ROOT / "scripts"]
    entries: list[FileEntry] = []
    for item in roots:
        if item.is_file():
            entries.append((item.resolve(), item.relative_to(ROOT).as_posix()))
        elif item.is_dir():
            for path in item.rglob("*"):
                if path.is_file() and not any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in path.parts):
                    entries.append((path.resolve(), path.relative_to(ROOT).as_posix()))
    if not demo_root.is_dir():
        raise ValueError(f"verified demo directory does not exist: {demo_root}")
    for path in demo_root.rglob("*"):
        if path.is_file():
            entries.append((path.resolve(), f"outputs/demo/{path.relative_to(demo_root).as_posix()}"))
    unique = {(path, archive_name): None for path, archive_name in entries}
    return sorted(unique, key=lambda entry: entry[1])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def security_scan(entries: list[FileEntry]) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    for path, archive_name in entries:
        if path.name in FORBIDDEN_FILE_NAMES or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
            findings.append({"path": archive_name, "rule": "forbidden_sensitive_file"})
        should_scan = path.suffix.lower() in TEXT_SUFFIXES or path.name in {".env.example", ".gitignore", ".dockerignore", "Dockerfile", "LICENSE"}
        if not should_scan:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"path": archive_name, "rule": name})
    return {"status": "PASS" if not findings else "FAIL", "findings": findings, "files_scanned": len(entries)}


def generate_sbom(entries: list[FileEntry]) -> dict[str, Any]:
    files = [
        {
            "SPDXID": f"SPDXRef-File-{index:04d}",
            "fileName": archive_name,
            "checksums": [{"algorithm": "SHA256", "checksumValue": _sha256(path)}],
        }
        for index, (path, archive_name) in enumerate(entries, start=1)
    ]
    payload = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "proofguard-autonomous-agent-0.2.0",
        "documentNamespace": "https://example.invalid/spdx/proofguard-autonomous-agent/0.2.0",
        "creationInfo": {"creators": ["Tool: proofguard-release-assets-v2"]},
        "packages": [{"name": "proofguard-autonomous-agent", "SPDXID": "SPDXRef-Package", "versionInfo": "0.2.0", "downloadLocation": "NOASSERTION", "filesAnalyzed": True, "licenseConcluded": "MIT", "licenseDeclared": "MIT"}],
        "files": files,
        "relationships": [{"spdxElementId": "SPDXRef-Package", "relationshipType": "CONTAINS", "relatedSpdxElement": entry["SPDXID"]} for entry in files],
    }
    DIST.mkdir(parents=True, exist_ok=True)
    SBOM.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"path": SBOM.relative_to(ROOT).as_posix(), "sha256": _sha256(SBOM), "file_count": len(files)}


def _write_zip_entry(archive: zipfile.ZipFile, archive_name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(archive_name, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data)


def build_judge_pack(entries: list[FileEntry]) -> dict[str, Any]:
    pack_entries = [*entries, (SBOM.resolve(), SBOM.relative_to(ROOT).as_posix())]
    pack_entries.sort(key=lambda entry: entry[1])
    DIST.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(JUDGE_PACK, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, archive_name in pack_entries:
            data = path.read_bytes()
            _write_zip_entry(archive, archive_name, data)
            manifest_entries.append({"path": archive_name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
        manifest_data = (json.dumps({"schema": "proofguard.judge-pack-manifest.v2", "files": manifest_entries}, indent=2) + "\n").encode("utf-8")
        _write_zip_entry(archive, "JUDGE_PACK_MANIFEST.json", manifest_data)
    return {"path": JUDGE_PACK.relative_to(ROOT).as_posix(), "sha256": _sha256(JUDGE_PACK), "bytes": JUDGE_PACK.stat().st_size, "file_count": len(pack_entries) + 1}


def verify_judge_pack(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                errors.append("duplicate archive paths")
            if "JUDGE_PACK_MANIFEST.json" not in names:
                errors.append("JUDGE_PACK_MANIFEST.json missing")
                manifest: dict[str, Any] = {}
            else:
                manifest = json.loads(archive.read("JUDGE_PACK_MANIFEST.json").decode("utf-8"))
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if not isinstance(files, list):
                errors.append("judge-pack manifest files must be a list")
                files = []
            expected_names = {"JUDGE_PACK_MANIFEST.json"}
            for item in files:
                if not isinstance(item, dict):
                    errors.append("invalid manifest entry")
                    continue
                archive_name = str(item.get("path", ""))
                relative = Path(archive_name)
                if not archive_name or relative.is_absolute() or ".." in relative.parts:
                    errors.append(f"unsafe manifest path: {archive_name}")
                    continue
                expected_names.add(archive_name)
                if archive_name not in names:
                    errors.append(f"missing archive member: {archive_name}")
                    continue
                data = archive.read(archive_name)
                if hashlib.sha256(data).hexdigest() != item.get("sha256"):
                    errors.append(f"sha256 mismatch: {archive_name}")
                if len(data) != item.get("bytes"):
                    errors.append(f"size mismatch: {archive_name}")
            unexpected = sorted(set(names) - expected_names)
            if unexpected:
                errors.append(f"unexpected archive members: {unexpected}")
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ProofGuard release security evidence, SPDX SBOM, and deterministic judge pack")
    parser.add_argument("--demo-root", type=Path, default=ROOT / "outputs" / "demo")
    args = parser.parse_args(argv)

    try:
        entries = _source_entries(args.demo_root.resolve())
        security = security_scan(entries)
        sbom = generate_sbom(entries)
        judge_pack = build_judge_pack(entries) if security["status"] == "PASS" else None
        judge_pack_verification = verify_judge_pack(JUDGE_PACK) if judge_pack is not None else None
        error = None
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        security = {"status": "FAIL", "findings": [], "files_scanned": 0}
        sbom = None
        judge_pack = None
        judge_pack_verification = None
        error = f"{type(exc).__name__}: {exc}"
    payload = {
        "status": "PASS" if security["status"] == "PASS" and judge_pack is not None and judge_pack_verification is not None and judge_pack_verification["status"] == "PASS" else "FAIL",
        "security": security,
        "sbom": sbom,
        "judge_pack": judge_pack,
        "judge_pack_verification": judge_pack_verification,
        "error": error,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
