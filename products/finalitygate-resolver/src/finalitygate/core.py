from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

FinalityState = Literal["OPEN", "PENDING_FINALITY", "WAIT_FOR_PROOF", "RESOLVE", "DISPUTE"]
ProofStatus = Literal["VALID", "MISSING", "INVALID", "UNVERIFIED"]
RootStatus = Literal["MATCH", "MISSING", "MISMATCH", "UNVERIFIED"]

FINAL_STATUSES = {"FINAL", "FINISHED", "FT", "AET", "PEN"}
NON_FINAL_STATUSES = {"SCHEDULED", "NOT_STARTED", "NS", "LIVE", "IN_PLAY", "HT", "PAUSED"}


def _iso(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class OutcomeMarket:
    market_id: str
    fixture_id: str
    market_type: Literal["MATCH_RESULT"]
    selections: tuple[str, ...]
    policy_version: str = "finalitygate-v1"

    def __post_init__(self) -> None:
        if not self.market_id.strip() or not self.fixture_id.strip():
            raise ValueError("market_id and fixture_id are required")
        normalized = tuple(selection.strip().upper() for selection in self.selections)
        if len(normalized) < 2 or len(set(normalized)) != len(normalized):
            raise ValueError("selections must contain at least two unique values")
        object.__setattr__(self, "selections", normalized)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["selections"] = list(self.selections)
        return row


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    fixture_id: str
    fixture_status: str
    home_score: int | None
    away_score: int | None
    declared_result: str | None
    observed_at: datetime
    proof_status: ProofStatus = "UNVERIFIED"
    root_status: RootStatus = "UNVERIFIED"
    proof_reference: str | None = None
    expected_root: str | None = None
    observed_root: str | None = None
    source_fingerprint: str | None = None

    def __post_init__(self) -> None:
        if self.home_score is not None and self.home_score < 0:
            raise ValueError("home_score cannot be negative")
        if self.away_score is not None and self.away_score < 0:
            raise ValueError("away_score cannot be negative")
        object.__setattr__(self, "fixture_status", self.fixture_status.strip().upper())
        if self.declared_result is not None:
            object.__setattr__(self, "declared_result", self.declared_result.strip().upper())

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["observed_at"] = _iso(self.observed_at)
        return row


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    state: FinalityState
    market_id: str
    fixture_id: str
    resolved_selection: str | None
    reasons: tuple[str, ...]
    checks: dict[str, Any]
    receipt_payload: dict[str, Any]
    receipt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "market_id": self.market_id,
            "fixture_id": self.fixture_id,
            "resolved_selection": self.resolved_selection,
            "reasons": list(self.reasons),
            "checks": self.checks,
            "receipt_payload": self.receipt_payload,
            "receipt_sha256": self.receipt_sha256,
        }


class FinalityGateResolver:
    def resolve(self, market: OutcomeMarket, evidence: ResolutionEvidence) -> ResolutionDecision:
        reasons: list[str] = []
        proof_reference_present = bool(evidence.proof_reference and evidence.proof_reference.strip())
        root_values_present = bool(evidence.expected_root and evidence.observed_root)
        checks: dict[str, Any] = {
            "fixture_identity_match": market.fixture_id == evidence.fixture_id,
            "fixture_final": evidence.fixture_status in FINAL_STATUSES,
            "score_complete": evidence.home_score is not None and evidence.away_score is not None,
            "declared_result_present": evidence.declared_result is not None,
            "declared_result_allowed": evidence.declared_result in market.selections if evidence.declared_result else False,
            "proof_status": evidence.proof_status,
            "proof_reference_present": proof_reference_present,
            "root_status": evidence.root_status,
            "root_values_present": root_values_present,
        }

        derived_result = self._derive_match_result(evidence.home_score, evidence.away_score)
        checks["derived_result"] = derived_result
        checks["declared_matches_score"] = (
            evidence.declared_result == derived_result
            if evidence.declared_result is not None and derived_result is not None
            else False
        )

        state: FinalityState
        resolved_selection: str | None = None

        if not checks["fixture_identity_match"]:
            state = "DISPUTE"
            reasons.append("fixture_identity_conflict")
        elif evidence.fixture_status in NON_FINAL_STATUSES:
            state = "OPEN" if evidence.fixture_status in {"SCHEDULED", "NOT_STARTED", "NS"} else "PENDING_FINALITY"
            reasons.append("fixture_not_final")
        elif not checks["fixture_final"]:
            state = "PENDING_FINALITY"
            reasons.append("unrecognized_or_nonfinal_fixture_status")
        elif not checks["score_complete"] or not checks["declared_result_present"]:
            state = "PENDING_FINALITY"
            reasons.append("final_result_incomplete")
        elif not checks["declared_result_allowed"]:
            state = "DISPUTE"
            reasons.append("declared_result_not_allowed_by_market")
        elif not checks["declared_matches_score"]:
            state = "DISPUTE"
            reasons.append("declared_result_conflicts_with_score")
        elif evidence.proof_status == "INVALID":
            state = "DISPUTE"
            reasons.append("proof_invalid")
        elif evidence.root_status == "MISMATCH":
            state = "DISPUTE"
            reasons.append("onchain_root_mismatch")
        elif self._root_values_conflict(evidence):
            state = "DISPUTE"
            reasons.append("declared_root_values_conflict")
        elif evidence.proof_status in {"MISSING", "UNVERIFIED"}:
            state = "WAIT_FOR_PROOF"
            reasons.append("proof_not_ready")
        elif evidence.proof_status == "VALID" and not proof_reference_present:
            state = "WAIT_FOR_PROOF"
            reasons.append("proof_reference_missing")
        elif evidence.root_status in {"MISSING", "UNVERIFIED"}:
            state = "WAIT_FOR_PROOF"
            reasons.append("onchain_root_not_confirmed")
        elif evidence.root_status == "MATCH" and not root_values_present:
            state = "WAIT_FOR_PROOF"
            reasons.append("root_values_missing")
        elif evidence.proof_status == "VALID" and evidence.root_status == "MATCH":
            state = "RESOLVE"
            resolved_selection = evidence.declared_result
            reasons.append("finality_result_proof_and_root_agree")
        else:
            state = "WAIT_FOR_PROOF"
            reasons.append("resolution_requirements_incomplete")

        checks["fail_closed"] = state != "RESOLVE" or (
            checks["fixture_identity_match"]
            and checks["fixture_final"]
            and checks["score_complete"]
            and checks["declared_result_allowed"]
            and checks["declared_matches_score"]
            and evidence.proof_status == "VALID"
            and proof_reference_present
            and evidence.root_status == "MATCH"
            and root_values_present
            and not self._root_values_conflict(evidence)
        )

        receipt_payload = {
            "schema": "finalitygate.resolution-receipt.v1",
            "market": market.to_dict(),
            "evidence": evidence.to_dict(),
            "state": state,
            "resolved_selection": resolved_selection,
            "reasons": reasons,
            "checks": checks,
        }
        receipt_sha256 = canonical_sha256(receipt_payload)
        return ResolutionDecision(
            state=state,
            market_id=market.market_id,
            fixture_id=market.fixture_id,
            resolved_selection=resolved_selection,
            reasons=tuple(reasons),
            checks=checks,
            receipt_payload=receipt_payload,
            receipt_sha256=receipt_sha256,
        )

    @staticmethod
    def _derive_match_result(home_score: int | None, away_score: int | None) -> str | None:
        if home_score is None or away_score is None:
            return None
        if home_score > away_score:
            return "HOME"
        if away_score > home_score:
            return "AWAY"
        return "DRAW"

    @staticmethod
    def _is_canonical_root(value: str | None) -> bool:
        """A canonical root is exactly 32 bytes rendered as 64 hex characters."""
        if not value:
            return False
        candidate = value
        if len(candidate) != 64:
            return False
        return all(character in "0123456789abcdefABCDEF" for character in candidate)

    @staticmethod
    def _root_values_conflict(evidence: ResolutionEvidence) -> bool:
        expected = evidence.expected_root
        observed = evidence.observed_root
        # A present root that is not a canonical 32-byte (64-hex) value is
        # invalid material and can never support a RESOLVE (fail-closed).
        for value in (expected, observed):
            if value and not FinalityGateResolver._is_canonical_root(value):
                return True
        if not expected or not observed:
            return False
        # Both values are canonical roots at this point; case-insensitive
        # comparison normalizes hex letter case without changing meaning.
        # No stripping: whitespace-padded roots are rejected as non-canonical above.
        return expected.lower() != observed.lower()


def _envelope_consistency_errors(payload: dict[str, Any], receipt: dict[str, Any]) -> list[str]:
    """Every duplicated envelope field must equal its bound value in receipt_payload.

    The receipt SHA-256 only covers ``receipt_payload``. The surrounding decision
    envelope repeats several bound fields (state, identifiers, selection, reasons,
    checks); if those are trusted without being checked, an envelope can be
    rewritten while still verifying. This closes that gap deterministically.
    Only fields actually present in the envelope are checked, so a bare
    ``{receipt_payload, receipt_sha256}`` payload still verifies.
    """

    market = receipt.get("market") if isinstance(receipt.get("market"), dict) else {}
    bindings = {
        "state": receipt.get("state"),
        "market_id": market.get("market_id"),
        "fixture_id": market.get("fixture_id"),
        "resolved_selection": receipt.get("resolved_selection"),
        "reasons": receipt.get("reasons"),
        "checks": receipt.get("checks"),
    }
    errors: list[str] = []
    for field, bound in bindings.items():
        if field in payload and payload[field] != bound:
            errors.append(f"envelope field '{field}' does not match receipt_payload")
    return errors


def verify_receipt(payload: dict[str, Any]) -> dict[str, Any]:
    receipt = payload.get("receipt_payload")
    expected = payload.get("receipt_sha256")
    if not isinstance(receipt, dict) or not isinstance(expected, str):
        return {"status": "FAIL", "errors": ["receipt_payload and receipt_sha256 are required"]}
    actual = canonical_sha256(receipt)
    errors: list[str] = [] if actual == expected else ["receipt_sha256 mismatch"]
    errors.extend(_envelope_consistency_errors(payload, receipt))
    return {"status": "PASS" if not errors else "FAIL", "errors": errors, "expected": expected, "actual": actual}
