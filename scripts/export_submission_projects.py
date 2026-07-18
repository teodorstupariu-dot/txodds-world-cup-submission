from __future__ import annotations

import ast
import hashlib
import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "exports"
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)

COMMON_REQUIRED_FILES = {
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "Dockerfile",
    "LICENSE",
    "README.md",
    "SUBMISSION.md",
    "pyproject.toml",
}

PROJECTS = {
    "proofguard-agent": {
        "root": ROOT / "products" / "proofguard-agent",
        "forbidden_import_prefixes": ("finalitygate", "txwc"),
        "required_files": COMMON_REQUIRED_FILES | {"render.yaml", "railway.json"},
    },
    "finalitygate-resolver": {
        "root": ROOT / "products" / "finalitygate-resolver",
        "forbidden_import_prefixes": ("proofguard_agent", "txwc"),
        "required_files": COMMON_REQUIRED_FILES | {"netlify.toml"},
    },
}

EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "outputs",
    "exports",
}

EXCLUDED_EXACT_NAMES = {
    "models.json",
    "RELEASE_REPORT.json",
    "id_rsa",
    "id_ed25519",
}

SENSITIVE_NAME_MARKERS = (
    "credential",
    "private-key",
    "private_key",
    "seed-phrase",
    "seed_phrase",
    "wallet",
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_excluded(path: Path, project_root: Path) -> bool:
    relative = path.relative_to(project_root)
    lower_name = path.name.lower()
    if path.name in EXCLUDED_EXACT_NAMES:
        return True
    if lower_name == ".env" or (lower_name.startswith(".env.") and lower_name != ".env.example"):
        return True
    if "token" in lower_name and path.suffix.lower() == ".json":
        return True
    if any(marker in lower_name for marker in SENSITIVE_NAME_MARKERS):
        return True
    if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
        return True
    if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx"}:
        return True
    return False


def _collect_files(project_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in project_root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"symlink is not allowed in export: {path}")
        if path.is_file() and not _is_excluded(path, project_root):
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(project_root).as_posix())


def _module_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return []


def _validate_independence(project_root: Path, forbidden_prefixes: tuple[str, ...]) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for path in sorted(project_root.rglob("*.py")):
        if _is_excluded(path, project_root):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            violations.append({
                "path": path.relative_to(project_root).as_posix(),
                "type": "parse_error",
                "detail": f"{type(exc).__name__}: {exc}",
            })
            continue
        for node in ast.walk(tree):
            for module in _module_names(node):
                if any(module == prefix or module.startswith(prefix + ".") for prefix in forbidden_prefixes):
                    violations.append({
                        "path": path.relative_to(project_root).as_posix(),
                        "type": "forbidden_cross_project_import",
                        "module": module,
                    })
    return violations


def _write_zip(project_name: str, project_root: Path, files: list[Path]) -> dict[str, Any]:
    EXPORTS.mkdir(parents=True, exist_ok=True)
    output = EXPORTS / f"{project_name}-source.zip"
    manifest_entries: list[dict[str, Any]] = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(project_root).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            manifest_entries.append({
                "path": relative,
                "sha256": _sha256_bytes(data),
                "bytes": len(data),
            })
        manifest = {
            "schema": "worldcup.standalone-source-export.v2",
            "project": project_name,
            "file_count": len(manifest_entries),
            "files": manifest_entries,
        }
        manifest_data = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        info = zipfile.ZipInfo("SOURCE_EXPORT_MANIFEST.json", date_time=FIXED_ZIP_TIME)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, manifest_data)
    return {
        "path": output.relative_to(ROOT).as_posix(),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "bytes": output.stat().st_size,
        "file_count": len(manifest_entries) + 1,
    }


def _verify_export(path: Path) -> dict[str, Any]:
    errors: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                errors.append("duplicate archive paths")
            if "SOURCE_EXPORT_MANIFEST.json" not in names:
                errors.append("SOURCE_EXPORT_MANIFEST.json missing")
                manifest: dict[str, Any] = {}
            else:
                manifest = json.loads(archive.read("SOURCE_EXPORT_MANIFEST.json").decode("utf-8"))
            files = manifest.get("files") if isinstance(manifest, dict) else None
            if not isinstance(files, list):
                errors.append("source manifest files must be a list")
                files = []
            expected_names = {"SOURCE_EXPORT_MANIFEST.json"}
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
                if _sha256_bytes(data) != item.get("sha256"):
                    errors.append(f"sha256 mismatch: {archive_name}")
                if len(data) != item.get("bytes"):
                    errors.append(f"size mismatch: {archive_name}")
            unexpected = sorted(set(names) - expected_names)
            if unexpected:
                errors.append(f"unexpected archive members: {unexpected}")
            if manifest.get("file_count") != len(files):
                errors.append("manifest file_count mismatch")
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return {"status": "PASS" if not errors else "FAIL", "errors": errors}


def main() -> int:
    if EXPORTS.exists():
        shutil.rmtree(EXPORTS)
    EXPORTS.mkdir(parents=True)

    results: dict[str, Any] = {}
    failures: list[str] = []
    for project_name, config in PROJECTS.items():
        project_root = Path(config["root"])
        if not project_root.is_dir():
            failures.append(f"missing project root: {project_root}")
            continue
        violations = _validate_independence(project_root, config["forbidden_import_prefixes"])
        files = _collect_files(project_root)
        present = {path.relative_to(project_root).as_posix() for path in files}
        required_files = set(config["required_files"])
        missing = sorted(required_files - present)
        if violations or missing:
            failures.append(f"{project_name}: violations={violations}; missing={missing}")
            results[project_name] = {
                "status": "FAIL",
                "independence_violations": violations,
                "missing_required_files": missing,
            }
            continue
        export = _write_zip(project_name, project_root, files)
        verification = _verify_export(ROOT / export["path"])
        project_status = "PASS" if verification["status"] == "PASS" else "FAIL"
        if project_status != "PASS":
            failures.append(f"{project_name}: export verification failed: {verification['errors']}")
        results[project_name] = {
            "status": project_status,
            "independence_violations": [],
            "missing_required_files": [],
            "required_files": sorted(required_files),
            "export": export,
            "verification": verification,
        }

    report = {
        "schema": "worldcup.submission-export-report.v2",
        "status": "PASS" if not failures and len(results) == len(PROJECTS) and all(item.get("status") == "PASS" for item in results.values()) else "FAIL",
        "projects": results,
        "failures": failures,
    }
    report_path = EXPORTS / "EXPORT_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
