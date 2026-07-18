from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .adapter import apply_score_finality, events_from_odds_payload
from .core import GENESIS_RECEIPT, ProofGuardAutonomousAgent, verify_receipt_chain
from .demo import run_demo, verify_demo
from .scenarios import SIMULATION_SCENARIOS, scenario_event, simulate_scenario
from .txline import TxLineClient, TxLineConfig, TxLineError
from .validation import inspect_odds_validation


def _write(payload: object) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _write_file(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _read_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_models(path: str | Path) -> dict[str, float]:
    payload = _read_json(path)
    result: dict[str, float] = {}
    for key, value in payload.items():
        number = float(value)
        if not 0.0 < number < 1.0:
            raise ValueError(f"model probability for {key!r} must be in (0, 1)")
        result[str(key)] = number
    return result


def _configured_live_agent(args: argparse.Namespace) -> ProofGuardAutonomousAgent:
    agent = ProofGuardAutonomousAgent()
    if getattr(args, "reduced_risk", False):
        agent.set_risk_mode("reduced")
    if getattr(args, "kill_switch", False):
        agent.set_kill_switch(True)
    return agent


def _fetch_cycle(
    client: TxLineClient,
    agent: ProofGuardAutonomousAgent,
    *,
    fixture_id: str,
    model_probabilities: dict[str, float],
    with_scores: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    odds_payload = client.odds_snapshot(fixture_id)
    events = events_from_odds_payload(
        odds_payload,
        fixture_id=fixture_id,
        model_probabilities=model_probabilities,
    )
    score_summary: dict[str, Any] = {
        "requested": with_scores,
        "available": False,
        "fixture_final": False,
        "winning_selection": None,
    }
    if with_scores:
        scores_payload = client.scores_snapshot(fixture_id)
        events, normalized_scores = apply_score_finality(events, scores_payload, fixture_id=fixture_id)
        score_summary = {"requested": True, "available": True, **normalized_scores}
    cycle = agent.process(events)
    source = {
        "origin": client.config.origin,
        "odds_schema_fingerprint": events[0].source_fingerprint if events else None,
        "score_summary": score_summary,
        "raw_payload_persisted": False,
    }
    return cycle, source


def _require_live_config() -> TxLineConfig:
    config = TxLineConfig.from_env()
    config.validate()
    if not config.guest_jwt or not config.api_token:
        raise ValueError("TXLINE_GUEST_JWT and TXLINE_API_TOKEN are required")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proofguard", description="Autonomous TxODDS paper-trading agent with non-bypassable integrity")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Show local/live configuration without exposing credentials")

    simulate = sub.add_parser("simulate", help="Run a preset market scenario (or all) through a fresh agent and show the integrity-gate decision")
    simulate.add_argument("--scenario", choices=sorted(SIMULATION_SCENARIOS), help="Scenario to run; omit to run every scenario")
    simulate.add_argument("--chain", action="store_true", help="Run every scenario through ONE agent so the receipts form a verifiable hash-linked chain (output feeds 'verify-chain')")
    simulate.add_argument("--out")

    verify_chain = sub.add_parser("verify-chain", help="Verify the hash-linked receipt chain inside a live-once/live-loop/simulate --chain output file")
    verify_chain.add_argument("--input", required=True, help="Path to a JSON output containing cycles[].records[] or records[]")

    demo = sub.add_parser("demo", help="Generate the deterministic no-wallet judge demo")
    demo.add_argument("--out", default="outputs/demo")

    verify = sub.add_parser("verify-demo", help="Verify generated demo artifact hashes and sizes")
    verify.add_argument("root", nargs="?", default="outputs/demo")

    inspect_validation = sub.add_parser("inspect-odds-validation", help="Strictly inspect an official /api/odds/validation response without claiming on-chain execution")
    inspect_validation.add_argument("--input", required=True)
    inspect_validation.add_argument("--out")

    live_validation = sub.add_parser("live-odds-validation", help="Fetch and inspect one authorized TxLINE odds-validation response without persisting the raw payload")
    live_validation.add_argument("--message-id", required=True)
    live_validation.add_argument("--ts", type=int, required=True)
    live_validation.add_argument("--out", default="outputs/live_odds_validation.json")

    live = sub.add_parser("live-once", help="Fetch one TxLINE snapshot and run one autonomous paper-decision cycle")
    live.add_argument("--fixture-id", required=True)
    live.add_argument("--models", required=True, help="JSON mapping of SELECTION or MARKET|SELECTION to model probability")
    live.add_argument("--out", default="outputs/live_once.json")
    live.add_argument("--with-scores", action="store_true", help="Also fetch score state and close paper positions when the fixture is final")
    live.add_argument("--reduced-risk", action="store_true")
    live.add_argument("--kill-switch", action="store_true")

    loop = sub.add_parser("live-loop", help="Poll TxLINE repeatedly and execute autonomous paper decisions without per-cycle approval")
    loop.add_argument("--fixture-id", required=True)
    loop.add_argument("--models", required=True)
    loop.add_argument("--out", default="outputs/live_loop.json")
    loop.add_argument("--cycles", type=int, default=10)
    loop.add_argument("--interval-seconds", type=float, default=5.0)
    loop.add_argument("--with-scores", action="store_true")
    loop.add_argument("--reduced-risk", action="store_true")
    loop.add_argument("--kill-switch", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            config = TxLineConfig.from_env()
            config.validate()
            _write({
                "status": "PASS",
                "config": config.status(),
                "live_ready": bool(config.guest_jwt and config.api_token),
                "secrets_exposed": False,
                "proof_boundary": "live API access does not itself establish on-chain odds validation",
            })
            return 0
        if args.command == "simulate":
            names = [args.scenario] if args.scenario else sorted(SIMULATION_SCENARIOS)
            if getattr(args, "chain", False):
                # Feed every scenario to ONE agent so the emitted receipts form a
                # genuine append-only, hash-linked chain that 'verify-chain' proves.
                agent = ProofGuardAutonomousAgent()
                cycle = agent.process([scenario_event(name) for name in names])
                verdict = verify_receipt_chain(cycle["records"], genesis=GENESIS_RECEIPT)
                payload = {
                    "status": "PASS",
                    "mode": "chain",
                    "count": len(cycle["records"]),
                    "scenarios": [
                        {
                            "scenario": name,
                            "integrity": record["integrity"]["decision"],
                            "action": record["action"],
                        }
                        for name, record in zip(names, cycle["records"])
                    ],
                    "chain_verification": verdict,
                    "records": cycle["records"],
                }
                if getattr(args, "out", None):
                    target = _write_file(args.out, payload)
                    _write({"status": "PASS", "out": str(target.resolve()), "count": payload["count"], "chain_verification": verdict})
                else:
                    _write(payload)
                return 0 if verdict["status"] == "PASS" else 1
            results = [simulate_scenario(name) for name in names]
            summary = [
                {
                    "scenario": item["scenario"],
                    "label": item["label"],
                    "integrity": item["decision"]["integrity"]["decision"],
                    "action": item["decision"]["action"],
                    "reasons": item["decision"]["reasons"],
                }
                for item in results
            ]
            payload = {"status": "PASS", "count": len(results), "scenarios": summary, "records": [r["decision"] for r in results]}
            if getattr(args, "out", None):
                target = _write_file(args.out, payload)
                _write({"status": "PASS", "out": str(target.resolve()), "count": len(results), "scenarios": summary})
            else:
                _write(payload)
            return 0
        if args.command == "verify-chain":
            source = _read_json(args.input)
            records: list[dict[str, Any]] = []
            if isinstance(source.get("cycles"), list):
                for cycle in source["cycles"]:
                    if isinstance(cycle, dict) and isinstance(cycle.get("records"), list):
                        records.extend(cycle["records"])
            elif isinstance(source.get("records"), list):
                records = list(source["records"])
            else:
                raise ValueError("input must contain cycles[].records[] or records[]")
            genesis = records[0].get("prev_receipt_sha256", GENESIS_RECEIPT) if records else GENESIS_RECEIPT
            verdict = verify_receipt_chain(records, genesis=genesis)
            verdict["is_genesis_anchored"] = genesis == GENESIS_RECEIPT
            _write(verdict)
            return 0 if verdict["status"] == "PASS" else 1
        if args.command == "demo":
            result = run_demo(args.out)
            _write({
                "status": result["status"],
                "out": str(Path(args.out).resolve()),
                "checks": result["checks"],
                "bundle_sha256": result["manifest"]["bundle_sha256"],
            })
            return 0 if result["status"] == "PASS" else 1
        if args.command == "verify-demo":
            result = verify_demo(args.root)
            _write(result)
            return 0 if result["status"] == "PASS" else 1
        if args.command == "inspect-odds-validation":
            result = inspect_odds_validation(_read_json(args.input))
            if args.out:
                _write_file(args.out, result)
                _write({
                    "status": result["status"],
                    "out": str(Path(args.out).resolve()),
                    "structural_fingerprint_sha256": result.get("structural_fingerprint_sha256"),
                    "exact_leaf_serialization_executed": False,
                    "onchain_validate_odds_executed": False,
                })
            else:
                _write(result)
            return 0 if result["status"] == "PASS" else 1
        if args.command == "live-odds-validation":
            config = _require_live_config()
            raw = TxLineClient(config).odds_validation(message_id=args.message_id, ts=args.ts)
            inspection = inspect_odds_validation(raw)
            payload = {
                "status": inspection["status"],
                "source": {
                    "origin": config.origin,
                    "message_id": args.message_id,
                    "timestamp": args.ts,
                    "raw_payload_persisted": False,
                    "credentials_exposed": False,
                },
                "inspection": inspection,
            }
            target = _write_file(args.out, payload)
            _write({
                "status": payload["status"],
                "out": str(target.resolve()),
                "raw_payload_persisted": False,
                "exact_leaf_serialization_executed": False,
                "onchain_validate_odds_executed": False,
                "structural_fingerprint_sha256": inspection.get("structural_fingerprint_sha256"),
            })
            return 0 if payload["status"] == "PASS" else 1
        if args.command in {"live-once", "live-loop"}:
            config = _require_live_config()
            models = _read_models(args.models)
            client = TxLineClient(config)
            agent = _configured_live_agent(args)
            requested_cycles = 1 if args.command == "live-once" else int(args.cycles)
            interval = 0.0 if args.command == "live-once" else float(args.interval_seconds)
            if not 1 <= requested_cycles <= 10_000:
                raise ValueError("cycles must be between 1 and 10000")
            if not 0.0 <= interval <= 3600.0:
                raise ValueError("interval-seconds must be between 0 and 3600")

            cycles: list[dict[str, Any]] = []
            sources: list[dict[str, Any]] = []
            for index in range(requested_cycles):
                cycle, source = _fetch_cycle(
                    client,
                    agent,
                    fixture_id=args.fixture_id,
                    model_probabilities=models,
                    with_scores=args.with_scores,
                )
                cycles.append(cycle)
                sources.append(source)
                if source.get("score_summary", {}).get("fixture_final"):
                    break
                if index + 1 < requested_cycles and interval:
                    time.sleep(interval)

            unsafe_entry_count = sum(int(cycle["safety"]["unsafe_entry_count"]) for cycle in cycles)
            payload = {
                "status": "PASS",
                "mode": args.command,
                "fixture_id": args.fixture_id,
                "requested_cycles": requested_cycles,
                "executed_cycles": len(cycles),
                "interval_seconds": interval,
                "sources": sources,
                "cycles": cycles,
                "final_portfolio": agent.portfolio_snapshot(),
                "unsafe_entry_count": unsafe_entry_count,
                "credentials_exposed": False,
                "raw_payload_persisted": False,
            }
            target = _write_file(args.out, payload)
            _write({
                "status": "PASS",
                "out": str(target.resolve()),
                "executed_cycles": len(cycles),
                "unsafe_entry_count": unsafe_entry_count,
                "final_exposure": agent.total_exposure,
                "raw_payload_persisted": False,
            })
            return 0
    except (KeyError, TypeError, TxLineError, ValueError, OSError, json.JSONDecodeError) as exc:
        _write({
            "status": "FAIL",
            "error": f"{type(exc).__name__}: {exc}",
            "credentials_exposed": False,
            "raw_payload_persisted": False,
        })
        return 1
    return 2
