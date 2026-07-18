from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import FinalityGateResolver, OutcomeMarket, ResolutionEvidence, verify_receipt

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size,
    }


def _evidence(
    *,
    status: str,
    home: int | None,
    away: int | None,
    declared: str | None,
    proof: str,
    root: str,
    expected_root: str | None = None,
    observed_root: str | None = None,
) -> ResolutionEvidence:
    return ResolutionEvidence(
        fixture_id="wc-finality-001",
        fixture_status=status,
        home_score=home,
        away_score=away,
        declared_result=declared,
        observed_at=NOW,
        proof_status=proof,  # type: ignore[arg-type]
        root_status=root,  # type: ignore[arg-type]
        proof_reference="proof-batch-2026-001" if proof != "MISSING" else None,
        expected_root=expected_root,
        observed_root=observed_root,
        source_fingerprint="txline-schema-demo-v1",
    )


def build_demo_summary() -> dict[str, Any]:
    """Compute the deterministic demo summary in memory, writing nothing to disk.

    This is the single source of truth for the demo decisions and receipts. Both
    ``run_demo`` (which additionally persists artifacts) and the web application
    consume it, so the web startup performs no filesystem I/O.
    """

    resolver = FinalityGateResolver()
    market = OutcomeMarket(
        market_id="market-worldcup-match-result-001",
        fixture_id="wc-finality-001",
        market_type="MATCH_RESULT",
        selections=("HOME", "DRAW", "AWAY"),
    )

    cases = [
        (
            "open",
            _evidence(status="SCHEDULED", home=None, away=None, declared=None, proof="MISSING", root="MISSING"),
            "OPEN",
        ),
        (
            "pending_finality",
            _evidence(status="LIVE", home=1, away=0, declared="HOME", proof="UNVERIFIED", root="UNVERIFIED"),
            "PENDING_FINALITY",
        ),
        (
            "wait_for_proof",
            _evidence(status="FINAL", home=2, away=1, declared="HOME", proof="MISSING", root="MISSING"),
            "WAIT_FOR_PROOF",
        ),
        (
            "dispute_result_conflict",
            _evidence(status="FINAL", home=2, away=1, declared="AWAY", proof="VALID", root="MATCH"),
            "DISPUTE",
        ),
        (
            "dispute_root_mismatch",
            _evidence(
                status="FINAL",
                home=2,
                away=1,
                declared="HOME",
                proof="VALID",
                root="MATCH",
                expected_root="aa" * 32,
                observed_root="bb" * 32,
            ),
            "DISPUTE",
        ),
        (
            "resolve",
            _evidence(
                status="FINAL",
                home=2,
                away=1,
                declared="HOME",
                proof="VALID",
                root="MATCH",
                expected_root="cc" * 32,
                observed_root="cc" * 32,
            ),
            "RESOLVE",
        ),
    ]

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for name, evidence, expected_state in cases:
        decision = resolver.resolve(market, evidence)
        decision_row = decision.to_dict()
        verification = verify_receipt(decision_row)
        case_payload = {
            "name": name,
            "expected_state": expected_state,
            "decision": decision_row,
            "receipt_verification": verification,
        }
        rows.append(case_payload)
        if decision.state != expected_state:
            errors.append(f"{name}: expected {expected_state}, got {decision.state}")
        if verification["status"] != "PASS":
            errors.append(f"{name}: receipt verification failed")

    state_counts: dict[str, int] = {}
    for row in rows:
        state = row["decision"]["state"]
        state_counts[state] = state_counts.get(state, 0) + 1

    required_states = {"OPEN", "PENDING_FINALITY", "WAIT_FOR_PROOF", "RESOLVE", "DISPUTE"}
    missing_states = sorted(required_states - set(state_counts))
    if missing_states:
        errors.append(f"missing demo states: {missing_states}")

    return {
        "schema": "finalitygate.demo-summary.v1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "market": market.to_dict(),
        "state_counts": state_counts,
        "required_states": sorted(required_states),
        "case_count": len(rows),
        "cases": rows,
        "claim_boundary": "deterministic resolution prototype; no custody, escrow release, wagering, or real-money settlement",
    }


def run_demo(output_root: str | Path) -> dict[str, Any]:
    """Compute the deterministic demo and persist its artifacts to ``output_root``."""

    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    summary = build_demo_summary()

    artifacts: list[dict[str, Any]] = []
    for case_payload in summary["cases"]:
        case_path = root / "cases" / f"{case_payload['name']}.json"
        _write_json(case_path, case_payload)
        artifacts.append(_artifact(case_path, root))

    summary_path = root / "summary.json"
    _write_json(summary_path, summary)
    artifacts.append(_artifact(summary_path, root))

    index_path = root / "index.html"
    index_path.write_text(_render_html(summary), encoding="utf-8")
    artifacts.append(_artifact(index_path, root))

    manifest = {
        "schema": "finalitygate.artifact-manifest.v1",
        "artifacts": sorted(artifacts, key=lambda item: item["path"]),
    }
    manifest["bundle_sha256"] = hashlib.sha256(
        json.dumps(manifest["artifacts"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(root / "manifest.json", manifest)
    return {**summary, "manifest": manifest}


def verify_demo(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {"status": "FAIL", "errors": ["manifest.json missing"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "FAIL", "errors": [f"invalid manifest: {exc}"]}
    errors: list[str] = []
    for artifact in manifest.get("artifacts", []):
        relative = Path(str(artifact.get("path", "")))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            errors.append(f"unsafe artifact path: {relative}")
            continue
        path = root / relative
        if not path.is_file():
            errors.append(f"missing artifact: {relative}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact.get("sha256"):
            errors.append(f"sha256 mismatch: {relative}")
        if path.stat().st_size != artifact.get("bytes"):
            errors.append(f"size mismatch: {relative}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "artifact_count": len(manifest.get("artifacts", [])),
        "bundle_sha256": manifest.get("bundle_sha256"),
    }


def _render_html(summary: dict[str, Any]) -> str:
    rows = []
    for case in summary["cases"]:
        decision = case["decision"]
        rows.append(
            "<tr>"
            f"<td>{html.escape(case['name'])}</td>"
            f"<td><strong>{html.escape(decision['state'])}</strong></td>"
            f"<td>{html.escape(str(decision.get('resolved_selection') or '—'))}</td>"
            f"<td>{html.escape(', '.join(decision['reasons']))}</td>"
            f"<td><code>{html.escape(decision['receipt_sha256'])}</code></td>"
            f"<td><a href='cases/{html.escape(case['name'])}.json'>inspect</a></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>FinalityGate World Cup Resolver</title>
<style>
body{{margin:0;background:#07111f;color:#eef4ff;font:16px system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:40px 20px}}p{{color:#a8b7cf;line-height:1.6}}.panel{{overflow:auto;background:#101d31;border:1px solid #29405f;border-radius:16px}}table{{width:100%;border-collapse:collapse;min-width:900px}}th,td{{padding:13px;border-bottom:1px solid #29405f;text-align:left;vertical-align:top}}th{{background:#14243d}}code{{font-size:.75rem;color:#c7d2fe;word-break:break-all}}a{{color:#7aa2ff}}.badge{{display:inline-block;padding:6px 10px;border-radius:999px;background:#17365f}}
</style></head><body><main><span class='badge'>deterministic no-wallet judge demo</span><h1>FinalityGate World Cup Resolver</h1>
<p>Resolution is emitted only when market definition, fixture finality, result evidence, proof status, and root status agree. Missing evidence waits; conflicting evidence disputes.</p>
<div class='panel'><table><thead><tr><th>Case</th><th>State</th><th>Selection</th><th>Reason</th><th>Receipt SHA-256</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>No custody, escrow release, wagering, or real-money settlement. This deterministic prototype demonstrates the finality policy and evidence receipts.</p>
</main></body></html>"""
