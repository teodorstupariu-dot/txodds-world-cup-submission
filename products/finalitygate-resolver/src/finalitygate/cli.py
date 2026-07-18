from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .commitment import build_commitment, verify_proof
from .core import FinalityGateResolver, OutcomeMarket, ResolutionEvidence, verify_receipt
from .demo import build_demo_summary, run_demo, verify_demo
from .explain import explain_decision
from .ledger import build_ledger, verify_ledger
from .txline import TxLineClient, TxLineConfig, TxLineError
from .validation import inspect_score_stat_validation


def _read_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _write(path: str | Path | None, payload: Any) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if path is None:
        print(text, end="")
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _market(payload: dict[str, Any]) -> OutcomeMarket:
    return OutcomeMarket(
        market_id=str(payload["market_id"]),
        fixture_id=str(payload["fixture_id"]),
        market_type=str(payload.get("market_type", "MATCH_RESULT")),  # type: ignore[arg-type]
        selections=tuple(str(value) for value in payload["selections"]),
        policy_version=str(payload.get("policy_version", "finalitygate-v1")),
    )


def _evidence(payload: dict[str, Any]) -> ResolutionEvidence:
    observed = payload.get("observed_at")
    if not isinstance(observed, str):
        raise ValueError("evidence.observed_at must be an ISO-8601 string")
    observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
    return ResolutionEvidence(
        fixture_id=str(payload["fixture_id"]),
        fixture_status=str(payload["fixture_status"]),
        home_score=payload.get("home_score"),
        away_score=payload.get("away_score"),
        declared_result=payload.get("declared_result"),
        observed_at=observed_at,
        proof_status=str(payload.get("proof_status", "UNVERIFIED")),  # type: ignore[arg-type]
        root_status=str(payload.get("root_status", "UNVERIFIED")),  # type: ignore[arg-type]
        proof_reference=payload.get("proof_reference"),
        expected_root=payload.get("expected_root"),
        observed_root=payload.get("observed_root"),
        source_fingerprint=payload.get("source_fingerprint"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="finalitygate", description="Proof-aware World Cup outcome-market finality resolver")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Show local/live configuration without exposing credentials")

    demo = sub.add_parser("demo", help="Generate the deterministic no-wallet judge demo")
    demo.add_argument("--out", default="outputs/demo")

    verify_demo_parser = sub.add_parser("verify-demo", help="Verify generated demo artifact hashes and sizes")
    verify_demo_parser.add_argument("root", nargs="?", default="outputs/demo")

    resolve = sub.add_parser("resolve", help="Resolve one market/evidence JSON pair")
    resolve.add_argument("--market", required=True)
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--out")

    receipt = sub.add_parser("verify-receipt", help="Verify a resolution receipt SHA-256")
    receipt.add_argument("--input", required=True)
    receipt.add_argument("--out")

    commitment = sub.add_parser("commitment", help="Resolve one market/evidence pair and build its 32-byte Merkle settlement commitment with an inclusion-proof self-check")
    commitment.add_argument("--market", required=True)
    commitment.add_argument("--evidence", required=True)
    commitment.add_argument("--out")

    explain = sub.add_parser("explain", help="Resolve one market/evidence pair and emit an auditor-facing explanation (checks passed/failed, dispute taxonomy, remediation)")
    explain.add_argument("--market", required=True)
    explain.add_argument("--evidence", required=True)
    explain.add_argument("--out")

    ledger = sub.add_parser("ledger", help="Build and verify a hash-linked settlement ledger with a single batch Merkle root over the deterministic demo cases")
    ledger.add_argument("--out")

    inspect_validation = sub.add_parser("inspect-score-validation", help="Strictly inspect an official scores/stat-validation response without claiming on-chain execution")
    inspect_validation.add_argument("--input", required=True)
    inspect_validation.add_argument("--out")

    live_validation = sub.add_parser("live-score-validation", help="Fetch and inspect one authorized TxLINE scores/stat-validation response without persisting the raw payload")
    live_validation.add_argument("--fixture-id", required=True)
    live_validation.add_argument("--seq", type=int, required=True)
    live_validation.add_argument("--stat-key", type=int, required=True)
    live_validation.add_argument("--stat-key2", type=int)
    live_validation.add_argument("--out", default="outputs/live_score_validation.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            config = TxLineConfig.from_env()
            config.validate()
            _write(None, {
                "status": "PASS",
                "config": config.status(),
                "live_ready": bool(config.guest_jwt and config.api_token),
                "secrets_exposed": False,
                "proof_boundary": "live API access does not itself establish on-chain validation",
            })
            return 0
        if args.command == "demo":
            result = run_demo(args.out)
            _write(None, {
                "status": result["status"],
                "out": str(Path(args.out).resolve()),
                "state_counts": result["state_counts"],
                "bundle_sha256": result["manifest"]["bundle_sha256"],
            })
            return 0 if result["status"] == "PASS" else 1
        if args.command == "verify-demo":
            result = verify_demo(args.root)
            _write(None, result)
            return 0 if result["status"] == "PASS" else 1
        if args.command == "resolve":
            market = _market(_read_json(args.market))
            evidence = _evidence(_read_json(args.evidence))
            decision = FinalityGateResolver().resolve(market, evidence).to_dict()
            _write(args.out, decision)
            if args.out:
                _write(None, {"state": decision["state"], "out": str(Path(args.out).resolve()), "receipt_sha256": decision["receipt_sha256"]})
            return 0
        if args.command == "verify-receipt":
            result = verify_receipt(_read_json(args.input))
            _write(args.out, result)
            if args.out:
                _write(None, {"status": result["status"], "out": str(Path(args.out).resolve())})
            return 0 if result["status"] == "PASS" else 1
        if args.command == "commitment":
            market = _market(_read_json(args.market))
            evidence = _evidence(_read_json(args.evidence))
            decision = FinalityGateResolver().resolve(market, evidence).to_dict()
            commit = build_commitment(decision)
            fields = [leaf["field"] for leaf in commit["leaves"]]
            sample_field = "declared_result" if "declared_result" in fields else fields[0]
            index = fields.index(sample_field)
            leaf_hash = commit["leaves"][index]["leaf_hash"]
            verified = verify_proof(leaf_hash, commit["proofs"][sample_field], commit["root"])
            payload = {
                "state": decision["state"],
                "root": commit["root"],
                "commitment": commit,
                "inclusion_proof_self_check": {"field": sample_field, "leaf_hash": leaf_hash, "verified": verified},
            }
            _write(args.out, payload)
            if args.out:
                _write(None, {"state": decision["state"], "root": commit["root"], "inclusion_proof_verified": verified, "out": str(Path(args.out).resolve())})
            return 0 if verified else 1
        if args.command == "explain":
            market = _market(_read_json(args.market))
            evidence = _evidence(_read_json(args.evidence))
            decision = FinalityGateResolver().resolve(market, evidence).to_dict()
            result = explain_decision(decision)
            _write(args.out, result)
            if args.out:
                _write(None, {"state": decision["state"], "out": str(Path(args.out).resolve())})
            return 0
        if args.command == "ledger":
            decisions = [case["decision"] for case in build_demo_summary().get("cases", [])]
            built = build_ledger(decisions)
            verification = verify_ledger(built)
            payload = {"ledger": built, "verification": verification}
            _write(args.out, payload)
            if args.out:
                _write(None, {
                    "status": verification["status"],
                    "count": built.get("count"),
                    "batch_root": built.get("batch_root"),
                    "out": str(Path(args.out).resolve()),
                })
            return 0 if verification["status"] == "PASS" else 1
        if args.command == "inspect-score-validation":
            result = inspect_score_stat_validation(_read_json(args.input))
            _write(args.out, result)
            if args.out:
                _write(None, {
                    "status": result["status"],
                    "out": str(Path(args.out).resolve()),
                    "onchain_view_executed": False,
                    "structural_fingerprint_sha256": result.get("structural_fingerprint_sha256"),
                })
            return 0 if result["status"] == "PASS" else 1
        if args.command == "live-score-validation":
            config = TxLineConfig.from_env()
            config.validate()
            if not config.guest_jwt or not config.api_token:
                raise ValueError("TXLINE_GUEST_JWT and TXLINE_API_TOKEN are required")
            raw = TxLineClient(config).score_stat_validation(
                fixture_id=args.fixture_id,
                seq=args.seq,
                stat_key=args.stat_key,
                stat_key2=args.stat_key2,
            )
            inspection = inspect_score_stat_validation(raw)
            payload = {
                "status": inspection["status"],
                "source": {
                    "origin": config.origin,
                    "fixture_id": str(args.fixture_id),
                    "seq": args.seq,
                    "stat_key": args.stat_key,
                    "stat_key2": args.stat_key2,
                    "raw_payload_persisted": False,
                    "credentials_exposed": False,
                },
                "inspection": inspection,
            }
            _write(args.out, payload)
            _write(None, {
                "status": payload["status"],
                "out": str(Path(args.out).resolve()),
                "raw_payload_persisted": False,
                "onchain_view_executed": False,
                "structural_fingerprint_sha256": inspection.get("structural_fingerprint_sha256"),
            })
            return 0 if payload["status"] == "PASS" else 1
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError, TxLineError) as exc:
        _write(None, {
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "credentials_exposed": False,
            "raw_payload_persisted": False,
        })
        return 1
    return 2
