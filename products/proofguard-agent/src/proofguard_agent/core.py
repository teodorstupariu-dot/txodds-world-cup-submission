from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

IntegrityDecision = Literal["PASS", "REVIEW", "BLOCK"]
AgentAction = Literal["ENTER", "HOLD", "REJECT", "CLOSE"]
RiskMode = Literal["normal", "reduced"]


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Fixed genesis anchor for the hash-linked receipt chain (64 hex zeros = the
# "no previous receipt" sentinel). Deterministic so identical runs match.
GENESIS_RECEIPT = "0" * 64


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    fixture_id: str
    market: str
    selection: str
    market_probability: float
    model_probability: float
    market_probability_sum: float
    stale_seconds: float
    proof_ready: bool
    backwards_timestamp: bool
    observed_at: datetime
    fixture_final: bool = False
    winning_selection: str | None = None
    source_fingerprint: str | None = None
    # Vig-free (de-margined) probability for this selection, when the full book
    # was available to normalize. None => fall back to the raw implied one.
    fair_probability: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("market_probability", self.market_probability),
            ("model_probability", self.model_probability),
        ):
            if not 0.0 < float(value) < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.fair_probability is not None and not 0.0 < float(self.fair_probability) < 1.0:
            raise ValueError("fair_probability must be in (0, 1)")
        if self.market_probability_sum <= 0:
            raise ValueError("market_probability_sum must be positive")
        if self.stale_seconds < 0:
            raise ValueError("stale_seconds cannot be negative")
        object.__setattr__(self, "selection", self.selection.strip().upper())
        if self.winning_selection is not None:
            object.__setattr__(self, "winning_selection", self.winning_selection.strip().upper())

    @property
    def effective_market_probability(self) -> float:
        """De-vigged market probability when available, else the raw implied one.

        The agent's edge is measured against the *fair* probability so the book's
        bookmaker margin (overround) is not mistaken for model edge. The raw
        implied probability and the overround stay available for integrity checks.
        """
        return self.fair_probability if self.fair_probability is not None else self.market_probability

    @property
    def overround(self) -> float:
        """Bookmaker margin of the book this selection came from (sum - 1)."""
        return self.market_probability_sum - 1.0

    def key(self) -> tuple[str, str, str]:
        return self.fixture_id, self.market, self.selection

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["observed_at"] = _iso(self.observed_at)
        return row


@dataclass(frozen=True, slots=True)
class IntegrityAssessment:
    decision: IntegrityDecision
    score: float
    reasons: tuple[str, ...]
    checks: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "score": round(self.score, 6),
            "reasons": list(self.reasons),
            "checks": self.checks,
        }


@dataclass(frozen=True, slots=True)
class PaperPosition:
    fixture_id: str
    market: str
    selection: str
    stake_fraction: float
    opened_at: datetime
    updated_at: datetime

    def key(self) -> tuple[str, str, str]:
        return self.fixture_id, self.market, self.selection

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["opened_at"] = _iso(self.opened_at)
        row["updated_at"] = _iso(self.updated_at)
        return row


class ProofGuardAutonomousAgent:
    """Autonomous, deterministic paper-trading agent with fail-closed integrity.

    This class never places real trades. It maintains only a normalized paper
    exposure ledger and deterministic receipts for judge/replay evaluation.
    """

    def __init__(
        self,
        *,
        minimum_edge: float = 0.03,
        confidence_floor: float = 0.60,
        maximum_stake_fraction: float = 0.02,
        maximum_total_exposure: float = 0.08,
        reduced_risk_multiplier: float = 0.35,
        review_stale_seconds: float = 90.0,
        block_stale_seconds: float = 180.0,
        sum_review_tolerance: float = 0.035,
        sum_block_tolerance: float = 0.08,
        proof_required: bool = True,
        kelly_fraction: float = 0.5,
        auto_controls: bool = True,
        integrity_flag_threshold: int = 2,
        integrity_block_storm_threshold: int = 3,
        policy_version: str = "proofguard-agent-v1",
    ) -> None:
        if not 0.0 <= minimum_edge < 1.0:
            raise ValueError("minimum_edge must be in [0, 1)")
        if not 0.0 <= confidence_floor <= 1.0:
            raise ValueError("confidence_floor must be in [0, 1]")
        if not 0.0 < maximum_stake_fraction <= maximum_total_exposure <= 1.0:
            raise ValueError("stake and exposure limits are inconsistent")
        if not 0.0 < reduced_risk_multiplier <= 1.0:
            raise ValueError("reduced_risk_multiplier must be in (0, 1]")
        if not 0.0 <= review_stale_seconds <= block_stale_seconds:
            raise ValueError("stale thresholds are inconsistent")
        if not 0.0 <= sum_review_tolerance <= sum_block_tolerance:
            raise ValueError("probability-sum thresholds are inconsistent")
        if not 0.0 < kelly_fraction <= 1.0:
            raise ValueError("kelly_fraction must be in (0, 1]")
        if integrity_flag_threshold < 1 or integrity_block_storm_threshold < 1:
            raise ValueError("integrity thresholds must be >= 1")
        self.minimum_edge = float(minimum_edge)
        self.confidence_floor = float(confidence_floor)
        self.maximum_stake_fraction = float(maximum_stake_fraction)
        self.maximum_total_exposure = float(maximum_total_exposure)
        self.reduced_risk_multiplier = float(reduced_risk_multiplier)
        self.review_stale_seconds = float(review_stale_seconds)
        self.block_stale_seconds = float(block_stale_seconds)
        self.sum_review_tolerance = float(sum_review_tolerance)
        self.sum_block_tolerance = float(sum_block_tolerance)
        self.proof_required = bool(proof_required)
        self.kelly_fraction = float(kelly_fraction)
        self.auto_controls = bool(auto_controls)
        self.integrity_flag_threshold = int(integrity_flag_threshold)
        self.integrity_block_storm_threshold = int(integrity_block_storm_threshold)
        self.policy_version = policy_version
        self.risk_mode: RiskMode = "normal"
        self.kill_switch = False
        # Autonomous risk-control state, driven by observed integrity health.
        self._consecutive_integrity_flags = 0   # REVIEW or BLOCK cycles in a row
        self._consecutive_integrity_blocks = 0  # BLOCK cycles in a row
        self._auto_reduced_engaged = False
        self._auto_kill_engaged = False
        self._positions: dict[tuple[str, str, str], PaperPosition] = {}
        self._cycle = 0
        # Hash-linked receipt chain: every decision receipt commits to the
        # previous receipt's hash, so the full decision history is an
        # append-only, tamper-evident ledger (reordering or editing any past
        # decision breaks the chain). A fresh agent starts from a fixed genesis
        # so two identical runs produce byte-identical chains (reproducible).
        self._genesis_receipt = GENESIS_RECEIPT
        self._last_receipt_sha256 = GENESIS_RECEIPT
        self._receipt_sequence = 0

    @property
    def total_exposure(self) -> float:
        return round(sum(position.stake_fraction for position in self._positions.values()), 12)

    @property
    def positions(self) -> tuple[PaperPosition, ...]:
        return tuple(self._positions[key] for key in sorted(self._positions))

    def set_risk_mode(self, mode: RiskMode) -> None:
        if mode not in {"normal", "reduced"}:
            raise ValueError("risk mode must be normal or reduced")
        self.risk_mode = mode

    def set_kill_switch(self, enabled: bool) -> None:
        self.kill_switch = bool(enabled)

    def assess_integrity(self, event: MarketEvent) -> IntegrityAssessment:
        reasons: list[str] = []
        critical = 0
        warnings = 0
        sum_deviation = abs(event.market_probability_sum - 1.0)

        if event.backwards_timestamp:
            reasons.append("backwards_timestamp")
            critical += 1
        if event.stale_seconds > self.block_stale_seconds:
            reasons.append("stale_update_block")
            critical += 1
        elif event.stale_seconds > self.review_stale_seconds:
            reasons.append("stale_update_review")
            warnings += 1
        if sum_deviation > self.sum_block_tolerance:
            reasons.append("market_probability_sum_block")
            critical += 1
        elif sum_deviation > self.sum_review_tolerance:
            reasons.append("market_probability_sum_review")
            warnings += 1
        if self.proof_required and not event.proof_ready:
            reasons.append("proof_not_ready")
            warnings += 1

        if critical:
            decision: IntegrityDecision = "BLOCK"
        elif warnings:
            decision = "REVIEW"
        else:
            decision = "PASS"
        score = max(0.0, 100.0 - critical * 45.0 - warnings * 18.0)
        return IntegrityAssessment(
            decision=decision,
            score=score,
            reasons=tuple(reasons or ["integrity_checks_passed"]),
            checks={
                "backwards_timestamp": event.backwards_timestamp,
                "stale_seconds": event.stale_seconds,
                "market_probability_sum": event.market_probability_sum,
                "sum_deviation": round(sum_deviation, 6),
                "proof_ready": event.proof_ready,
            },
        )

    def process(self, events: list[MarketEvent]) -> dict[str, Any]:
        self._cycle += 1
        records: list[dict[str, Any]] = []
        close_records: list[dict[str, Any]] = []

        if self.kill_switch:
            for key in sorted(tuple(self._positions)):
                position = self._positions.pop(key)
                close_records.append({
                    "action": "CLOSE",
                    "reason": "kill_switch_active",
                    "position_before": position.to_dict(),
                    "position_after": None,
                })

        for event in events:
            integrity = self.assess_integrity(event)
            position_before = self._positions.get(event.key())

            if event.fixture_final:
                action: AgentAction = "CLOSE"
                reasons = ["fixture_final_paper_position_close"]
                execution = self._close_fixture_positions(event.fixture_id)
                position_after = None
                edge = event.model_probability - event.effective_market_probability
                confidence = 0.0
                target_stake = 0.0
            else:
                edge = event.model_probability - event.effective_market_probability
                confidence = max(0.0, min(1.0, 0.5 + min(0.4, abs(edge) * 2.0)))
                action, reasons = self._decide(integrity, edge, confidence)
                execution, position_after, target_stake = self._execute(event, action, position_before)

            receipt_payload = {
                "schema": "proofguard.autonomous-decision-receipt.v2",
                "sequence": self._receipt_sequence,
                "prev_receipt_sha256": self._last_receipt_sha256,
                "cycle": self._cycle,
                "policy_version": self.policy_version,
                "controls": {
                    "risk_mode": self.risk_mode,
                    "kill_switch": self.kill_switch,
                    "minimum_edge": self.minimum_edge,
                    "confidence_floor": self.confidence_floor,
                    "maximum_stake_fraction": self.maximum_stake_fraction,
                    "maximum_total_exposure": self.maximum_total_exposure,
                    "kelly_fraction": self.kelly_fraction,
                },
                "event": event.to_dict(),
                "integrity": integrity.to_dict(),
                "signal": {
                    "edge": round(edge, 6),
                    "confidence": round(confidence, 6),
                    "fair_probability": round(event.effective_market_probability, 6),
                    "raw_market_probability": round(event.market_probability, 6),
                    "overround": round(event.overround, 6),
                },
                "action": action,
                "reasons": reasons,
                "execution": execution,
                "target_stake_fraction": target_stake,
                "position_before": position_before.to_dict() if position_before else None,
                "position_after": position_after.to_dict() if position_after else None,
                "portfolio_exposure_after": self.total_exposure,
            }
            receipt_sha256 = canonical_sha256(receipt_payload)
            records.append({**receipt_payload, "receipt_sha256": receipt_sha256})
            # Advance the append-only chain: this receipt becomes the parent of
            # the next one, in-cycle and across cycles.
            self._last_receipt_sha256 = receipt_sha256
            self._receipt_sequence += 1

        unsafe = [
            record for record in records
            if record["action"] == "ENTER" and record["integrity"]["decision"] in {"REVIEW", "BLOCK"}
        ]
        if unsafe:
            raise RuntimeError("safety invariant violated: unsafe ENTER")
        if self.total_exposure > self.maximum_total_exposure + 1e-12:
            raise RuntimeError("safety invariant violated: exposure cap")

        auto_control_actions = self._update_auto_controls(records)

        return {
            "schema": "proofguard.autonomous-cycle.v1",
            "status": "PASS",
            "cycle": self._cycle,
            "records": records,
            "kill_switch_closures": close_records,
            "auto_control_actions": auto_control_actions,
            "portfolio": self.portfolio_snapshot(),
            "receipt_chain": {
                "genesis": self._genesis_receipt,
                "length": self._receipt_sequence,
                "head_sha256": self._last_receipt_sha256,
            },
            "safety": {
                "unsafe_entry_count": 0,
                "integrity_non_bypassable": True,
                "exposure_within_limit": True,
                "receipt_chain_linked": True,
            },
        }

    def portfolio_snapshot(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "risk_mode": self.risk_mode,
            "kill_switch": self.kill_switch,
            "maximum_total_exposure": self.maximum_total_exposure,
            "total_exposure": self.total_exposure,
            "open_position_count": len(self._positions),
            "positions": [position.to_dict() for position in self.positions],
            "auto_controls": {
                "enabled": self.auto_controls,
                "kelly_fraction": self.kelly_fraction,
                "consecutive_integrity_flags": self._consecutive_integrity_flags,
                "consecutive_integrity_blocks": self._consecutive_integrity_blocks,
                "auto_reduced_engaged": self._auto_reduced_engaged,
                "auto_kill_engaged": self._auto_kill_engaged,
            },
        }

    def _decide(self, integrity: IntegrityAssessment, edge: float, confidence: float) -> tuple[AgentAction, list[str]]:
        if integrity.decision == "BLOCK":
            return "REJECT", ["blocked_by_integrity_policy"]
        if integrity.decision == "REVIEW":
            return "HOLD", ["integrity_review_requires_no_entry"]
        if self.kill_switch:
            return "REJECT", ["kill_switch_active"]
        if edge < self.minimum_edge:
            return "HOLD", ["edge_below_threshold"]
        if confidence < self.confidence_floor:
            return "HOLD", ["confidence_below_threshold"]
        return "ENTER", ["signal_and_integrity_policy_passed"]

    def _kelly_stake(self, event: MarketEvent) -> float:
        """Fractional-Kelly stake for a back bet at the raw market decimal odds.

        With decimal odds ``d = 1/raw_market_probability``, net odds ``b = d - 1``
        and win probability ``p = model_probability``, the Kelly fraction is
        ``f* = (b*p - (1-p)) / b``. We stake ``kelly_fraction`` of ``f*`` (half by
        default) and hard-cap at ``maximum_stake_fraction``. A non-positive edge
        yields zero. This ties bet size to a principled edge/odds trade-off
        instead of a flat heuristic.
        """
        model = event.model_probability
        raw_market = event.market_probability
        if not 0.0 < raw_market < 1.0:
            return 0.0
        net_odds = (1.0 / raw_market) - 1.0
        if net_odds <= 0.0:
            return 0.0
        kelly = (net_odds * model - (1.0 - model)) / net_odds
        if kelly <= 0.0:
            return 0.0
        return min(self.maximum_stake_fraction, self.kelly_fraction * kelly)

    def _target_stake(self, event: MarketEvent) -> float:
        raw = self._kelly_stake(event)
        if self.risk_mode == "reduced":
            raw *= self.reduced_risk_multiplier
        current = self._positions.get(event.key())
        exposure_without_current = self.total_exposure - (current.stake_fraction if current else 0.0)
        available = max(0.0, self.maximum_total_exposure - exposure_without_current)
        return round(min(raw, available), 12)

    def _update_auto_controls(self, records: list[dict[str, Any]]) -> list[str]:
        """React to observed integrity health after a cycle.

        Auto-engages reduced-risk on a streak of REVIEW/BLOCK cycles (released
        when integrity is clean again) and auto-engages the kill switch on an
        integrity *storm* (consecutive BLOCK cycles). The kill switch is sticky:
        an operator must clear it explicitly. Never relaxes controls a human set.
        """

        actions: list[str] = []
        decisions = [record["integrity"]["decision"] for record in records if "integrity" in record]
        if not decisions:
            return actions
        had_block = "BLOCK" in decisions
        had_flag = had_block or "REVIEW" in decisions
        self._consecutive_integrity_blocks = self._consecutive_integrity_blocks + 1 if had_block else 0
        self._consecutive_integrity_flags = self._consecutive_integrity_flags + 1 if had_flag else 0

        if not self.auto_controls:
            return actions
        if self._consecutive_integrity_flags >= self.integrity_flag_threshold and self.risk_mode == "normal":
            self.risk_mode = "reduced"
            self._auto_reduced_engaged = True
            actions.append("auto_reduced_risk_engaged")
        elif self._consecutive_integrity_flags == 0 and self._auto_reduced_engaged and self.risk_mode == "reduced":
            self.risk_mode = "normal"
            self._auto_reduced_engaged = False
            actions.append("auto_reduced_risk_released")
        if self._consecutive_integrity_blocks >= self.integrity_block_storm_threshold and not self.kill_switch:
            self.kill_switch = True
            self._auto_kill_engaged = True
            actions.append("auto_kill_switch_engaged_integrity_storm")
        return actions

    def _execute(
        self,
        event: MarketEvent,
        action: AgentAction,
        before: PaperPosition | None,
    ) -> tuple[str, PaperPosition | None, float]:
        if action == "ENTER":
            target = self._target_stake(event)
            if target <= 0.0:
                return "NOOP_EXPOSURE_CAP", before, 0.0
            after = PaperPosition(
                fixture_id=event.fixture_id,
                market=event.market,
                selection=event.selection,
                stake_fraction=target,
                opened_at=before.opened_at if before else event.observed_at,
                updated_at=event.observed_at,
            )
            self._positions[event.key()] = after
            return ("RESIZE" if before else "OPEN"), after, target
        if action == "REJECT" and before is not None:
            self._positions.pop(event.key(), None)
            return "CLOSE_SAFETY", None, 0.0
        if action == "HOLD" and before is not None:
            return "MAINTAIN", before, before.stake_fraction
        return "NOOP", before, before.stake_fraction if before else 0.0

    def _close_fixture_positions(self, fixture_id: str) -> list[dict[str, Any]]:
        closed: list[dict[str, Any]] = []
        for key in sorted(tuple(self._positions)):
            if key[0] != fixture_id:
                continue
            position = self._positions.pop(key)
            closed.append(position.to_dict())
        return closed


def verify_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    expected = payload.get("receipt_sha256")
    if not isinstance(expected, str):
        return {"status": "FAIL", "errors": ["receipt_sha256 missing"]}
    receipt_payload = {key: value for key, value in payload.items() if key != "receipt_sha256"}
    actual = canonical_sha256(receipt_payload)
    errors = [] if actual == expected else ["receipt_sha256 mismatch"]
    return {"status": "PASS" if not errors else "FAIL", "errors": errors, "expected": expected, "actual": actual}


def verify_receipt_chain(
    records: list[dict[str, Any]],
    *,
    genesis: str = GENESIS_RECEIPT,
) -> dict[str, Any]:
    """Verify an ordered list of decision receipts as an append-only chain.

    Independently reproves three properties without trusting any envelope field:

    1. **Integrity** — each receipt's ``receipt_sha256`` matches a fresh canonical
       hash of its own payload (any edit to a past decision is detected).
    2. **Linkage** — each receipt's ``prev_receipt_sha256`` equals the previous
       receipt's ``receipt_sha256`` (the first links to ``genesis``), so no
       decision can be silently inserted, removed, or reordered.
    3. **Ordering** — ``sequence`` increases by exactly one per receipt.

    Returns a machine-readable verdict a judge can run over the public receipts.
    """

    errors: list[str] = []
    expected_prev = genesis
    expected_sequence: int | None = None
    head_sha256 = genesis

    for index, record in enumerate(records):
        if not isinstance(record, dict):
            errors.append(f"record[{index}] is not an object")
            break

        single = verify_receipt(record)
        if single["status"] != "PASS":
            errors.append(f"record[{index}] integrity: {', '.join(single['errors'])}")

        prev = record.get("prev_receipt_sha256")
        if prev != expected_prev:
            errors.append(f"record[{index}] prev_receipt_sha256 breaks the chain")

        sequence = record.get("sequence")
        if expected_sequence is None:
            expected_sequence = sequence if isinstance(sequence, int) else 0
        if sequence != expected_sequence:
            errors.append(f"record[{index}] sequence is not contiguous")

        receipt_sha256 = record.get("receipt_sha256")
        expected_prev = receipt_sha256 if isinstance(receipt_sha256, str) else expected_prev
        head_sha256 = expected_prev
        expected_sequence = (expected_sequence or 0) + 1

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "checked": len(records),
        "genesis": genesis,
        "head_sha256": head_sha256,
    }
