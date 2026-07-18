"""Preset market scenarios for probing the integrity gate (web + CLI).

Each scenario is a single market snapshot run through a FRESH agent, so the
result is stateless and deterministic. Used by the web ``/api/simulate``
endpoint and the ``proofguard simulate`` CLI command.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .core import MarketEvent, ProofGuardAutonomousAgent

SIMULATION_SCENARIOS: dict[str, dict[str, Any]] = {
    "clean_value": {"label": "Clean feed · clear model edge", "selection": "HOME",
                    "market_probability": 0.45, "model_probability": 0.64},
    "low_edge": {"label": "Clean feed · edge too small", "selection": "HOME",
                 "market_probability": 0.60, "model_probability": 0.62},
    "stale_review": {"label": "Stale feed (>90s) · REVIEW", "selection": "HOME",
                     "market_probability": 0.55, "model_probability": 0.66, "stale_seconds": 120.0},
    "corrupt_block": {"label": "Corrupted book (attack) · BLOCK", "selection": "AWAY",
                      "market_probability": 0.25, "model_probability": 0.58, "probability_sum": 1.16,
                      "stale_seconds": 240.0, "proof_ready": False, "backwards": True},
    "fixture_final": {"label": "Full time · CLOSE", "selection": "HOME",
                      "market_probability": 0.95, "model_probability": 0.95,
                      "fixture_final": True, "winning_selection": "HOME"},
}


def scenario_event(scenario: str, *, observed_at: datetime | None = None) -> MarketEvent:
    """Build the deterministic MarketEvent for a named scenario."""
    spec = SIMULATION_SCENARIOS.get(scenario)
    if spec is None:
        raise KeyError(scenario)
    probability_sum = float(spec.get("probability_sum", 1.0))
    market_probability = float(spec["market_probability"])
    fair = min(max(market_probability / probability_sum, 1e-9), 1.0 - 1e-9) if probability_sum > 0 else None
    return MarketEvent(
        event_id=f"sim-{scenario}",
        fixture_id="wc-proofguard-sim-001",
        market="MATCH_RESULT",
        selection=spec["selection"],
        market_probability=market_probability,
        model_probability=float(spec["model_probability"]),
        market_probability_sum=probability_sum,
        stale_seconds=float(spec.get("stale_seconds", 10.0)),
        proof_ready=bool(spec.get("proof_ready", True)),
        backwards_timestamp=bool(spec.get("backwards", False)),
        observed_at=observed_at or datetime.now(timezone.utc),
        fixture_final=bool(spec.get("fixture_final", False)),
        winning_selection=spec.get("winning_selection"),
        fair_probability=fair,
        source_fingerprint="proofguard-sim-v1",
    )


def simulate_scenario(scenario: str) -> dict[str, Any]:
    """Run one preset scenario through a fresh agent and return the decision."""
    spec = SIMULATION_SCENARIOS[scenario] if scenario in SIMULATION_SCENARIOS else None
    if spec is None:
        raise KeyError(scenario)
    event = scenario_event(scenario)
    cycle = ProofGuardAutonomousAgent().process([event])
    return {"scenario": scenario, "label": spec["label"], "decision": cycle["records"][0]}
