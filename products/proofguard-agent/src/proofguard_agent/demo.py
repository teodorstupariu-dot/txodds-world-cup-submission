from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import MarketEvent, ProofGuardAutonomousAgent, verify_receipt

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


def _event(
    event_id: str,
    *,
    selection: str,
    market_probability: float,
    model_probability: float,
    probability_sum: float = 1.0,
    stale_seconds: float = 10.0,
    proof_ready: bool = True,
    backwards: bool = False,
    fixture_final: bool = False,
    winning_selection: str | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        fixture_id="wc-proofguard-001",
        market="MATCH_RESULT",
        selection=selection,
        market_probability=market_probability,
        model_probability=model_probability,
        market_probability_sum=probability_sum,
        stale_seconds=stale_seconds,
        proof_ready=proof_ready,
        backwards_timestamp=backwards,
        observed_at=NOW,
        fixture_final=fixture_final,
        winning_selection=winning_selection,
        source_fingerprint="txline-schema-demo-v1",
    )


def run_demo(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    agent = ProofGuardAutonomousAgent()
    cycles: list[dict[str, Any]] = []

    cycles.append(agent.process([_event("clean-enter", selection="HOME", market_probability=0.45, model_probability=0.65)]))

    agent.set_risk_mode("reduced")
    cycles.append(agent.process([_event("reduced-resize", selection="HOME", market_probability=0.45, model_probability=0.65)]))

    cycles.append(agent.process([_event("low-edge-hold", selection="DRAW", market_probability=0.30, model_probability=0.31)]))

    cycles.append(agent.process([_event(
        "integrity-reject",
        selection="AWAY",
        market_probability=0.25,
        model_probability=0.55,
        probability_sum=1.14,
        stale_seconds=240.0,
        proof_ready=False,
        backwards=True,
    )]))

    agent.set_kill_switch(True)
    cycles.append(agent.process([_event("kill-switch-reject", selection="HOME", market_probability=0.45, model_probability=0.65)]))

    agent.set_kill_switch(False)
    agent.set_risk_mode("normal")
    cycles.append(agent.process([_event("reopen", selection="HOME", market_probability=0.45, model_probability=0.65)]))
    cycles.append(agent.process([_event(
        "fixture-final-close",
        selection="HOME",
        market_probability=0.95,
        model_probability=0.95,
        fixture_final=True,
        winning_selection="HOME",
    )]))

    records = [record for cycle in cycles for record in cycle["records"]]
    actions = [record["action"] for record in records]
    executions = [record["execution"] for record in records]
    receipt_checks = [verify_receipt(record) for record in records]
    unsafe_entries = sum(int(cycle["safety"]["unsafe_entry_count"]) for cycle in cycles)

    checks = {
        "enter_demonstrated": "ENTER" in actions,
        "hold_demonstrated": "HOLD" in actions,
        "reject_demonstrated": "REJECT" in actions,
        "close_demonstrated": "CLOSE" in actions,
        "paper_open_demonstrated": "OPEN" in executions,
        "reduced_risk_resize_demonstrated": "RESIZE" in executions,
        "kill_switch_closed_positions": any(cycle["kill_switch_closures"] for cycle in cycles),
        "all_receipts_verify": all(check["status"] == "PASS" for check in receipt_checks),
        "unsafe_entry_count_zero": unsafe_entries == 0,
        "final_exposure_zero": agent.total_exposure == 0.0,
    }
    errors = [name for name, passed in checks.items() if not passed]

    summary = {
        "schema": "proofguard.demo-summary.v1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "checks": checks,
        "cycle_count": len(cycles),
        "cycles": cycles,
        "actions": actions,
        "executions": executions,
        "unsafe_entry_count": unsafe_entries,
        "final_portfolio": agent.portfolio_snapshot(),
        "claim_boundary": "autonomous simulated paper execution only; no wallet, wager, custody, real-money settlement, or profitability claim",
    }

    artifacts: list[dict[str, Any]] = []
    for index, cycle in enumerate(cycles, start=1):
        path = root / "cycles" / f"cycle-{index:02d}.json"
        _write_json(path, cycle)
        artifacts.append(_artifact(path, root))

    summary_path = root / "summary.json"
    _write_json(summary_path, summary)
    artifacts.append(_artifact(summary_path, root))

    index_path = root / "index.html"
    index_path.write_text(_render_html(summary), encoding="utf-8")
    artifacts.append(_artifact(index_path, root))

    manifest = {
        "schema": "proofguard.artifact-manifest.v1",
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
    for cycle_index, cycle in enumerate(summary["cycles"], start=1):
        for record in cycle["records"]:
            rows.append(
                "<tr>"
                f"<td>{cycle_index}</td>"
                f"<td>{html.escape(record['event']['event_id'])}</td>"
                f"<td><strong>{html.escape(record['action'])}</strong></td>"
                f"<td>{html.escape(record['integrity']['decision'])}</td>"
                f"<td>{html.escape(record['execution'] if isinstance(record['execution'], str) else 'FINAL_CLOSE')}</td>"
                f"<td>{record['signal']['edge']}</td>"
                f"<td><code>{html.escape(record['receipt_sha256'])}</code></td>"
                f"<td><a href='cycles/cycle-{cycle_index:02d}.json'>inspect</a></td>"
                "</tr>"
            )
    return f"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>ProofGuard Autonomous Agent</title>
<style>
body{{margin:0;background:#07111f;color:#eef4ff;font:16px system-ui,sans-serif}}main{{max-width:1180px;margin:auto;padding:40px 20px}}p{{color:#a8b7cf;line-height:1.6}}.panel{{overflow:auto;background:#101d31;border:1px solid #29405f;border-radius:16px}}table{{width:100%;border-collapse:collapse;min-width:980px}}th,td{{padding:13px;border-bottom:1px solid #29405f;text-align:left;vertical-align:top}}th{{background:#14243d}}code{{font-size:.72rem;color:#c7d2fe;word-break:break-all}}a{{color:#7aa2ff}}.badge{{display:inline-block;padding:6px 10px;border-radius:999px;background:#17365f}}
</style></head><body><main><span class='badge'>autonomous deterministic paper execution</span><h1>ProofGuard Autonomous Agent</h1>
<p>The signal engine is subordinate to a non-bypassable integrity policy. The demo covers ENTER, HOLD, REJECT, reduced-risk resizing, kill-switch closure, and fixture-final paper closure.</p>
<div class='panel'><table><thead><tr><th>Cycle</th><th>Event</th><th>Action</th><th>Integrity</th><th>Execution</th><th>Edge</th><th>Receipt SHA-256</th><th>Evidence</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>No wallet, wager, custody, real-money settlement, or profitability claim. Paper positions are normalized simulated exposure only.</p>
</main></body></html>"""
