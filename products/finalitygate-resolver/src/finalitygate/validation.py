from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

DAY_MILLISECONDS = 24 * 60 * 60 * 1000
DAILY_SCORES_SEED = b"daily_scores_roots"


def _integer(value: Any, field: str, errors: list[str]) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return None
    return number


def _bytes32_hex(value: Any, field: str, errors: list[str]) -> str | None:
    raw: bytes
    if isinstance(value, list):
        normalized: list[int] = []
        try:
            for item in value:
                number = int(item)
                if not -128 <= number <= 255:
                    raise OverflowError(number)
                normalized.append(number % 256)
            raw = bytes(normalized)
        except (TypeError, ValueError, OverflowError):
            errors.append(f"{field} must contain signed or unsigned byte values")
            return None
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        text = value.strip()
        try:
            if text.startswith("0x"):
                raw = bytes.fromhex(text[2:])
            elif text and len(text) % 2 == 0 and all(character in "0123456789abcdefABCDEF" for character in text):
                raw = bytes.fromhex(text)
            else:
                raw = base64.b64decode(text, validate=True)
        except (ValueError, binascii.Error):
            errors.append(f"{field} must be 32-byte hex, base64, or byte array")
            return None
    else:
        errors.append(f"{field} must be 32-byte hex, base64, or byte array")
        return None
    if len(raw) != 32:
        errors.append(f"{field} must contain exactly 32 bytes, received {len(raw)}")
        return None
    return raw.hex()


def _proof_nodes(value: Any, field: str, errors: list[str]) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        errors.append(f"{field} must be a list")
        return None
    normalized: list[dict[str, Any]] = []
    for index, node in enumerate(value):
        if not isinstance(node, dict):
            errors.append(f"{field}[{index}] must be an object")
            continue
        hash_hex = _bytes32_hex(node.get("hash"), f"{field}[{index}].hash", errors)
        direction = node.get("isRightSibling")
        if not isinstance(direction, bool):
            errors.append(f"{field}[{index}].isRightSibling must be boolean")
            continue
        if hash_hex is not None:
            normalized.append({"hash_hex": hash_hex, "is_right_sibling": direction})
    return normalized


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inspect_score_stat_validation(payload: Any) -> dict[str, Any]:
    """Validate and normalize the official TxODDS scores/stat-validation shape.

    This performs strict structural validation and derives the documented
    daily-scores PDA seed inputs. It does not execute the Solana program's
    read-only ``validateStat(...).view()`` method and therefore must not be
    described as complete on-chain proof verification.
    """

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return {
            "status": "FAIL",
            "errors": ["validation payload must be an object"],
            "warnings": [],
            "claim_boundary": "structural inspection only; no Solana view execution",
        }

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
        summary = {}
    update_stats = summary.get("updateStats")
    if not isinstance(update_stats, dict):
        errors.append("summary.updateStats must be an object")
        update_stats = {}

    fixture_id = summary.get("fixtureId")
    if fixture_id in (None, ""):
        errors.append("summary.fixtureId is required")

    update_count = _integer(update_stats.get("updateCount"), "summary.updateStats.updateCount", errors)
    min_timestamp = _integer(update_stats.get("minTimestamp"), "summary.updateStats.minTimestamp", errors)
    max_timestamp = _integer(update_stats.get("maxTimestamp"), "summary.updateStats.maxTimestamp", errors)
    if update_count is not None and update_count < 0:
        errors.append("summary.updateStats.updateCount cannot be negative")
    if min_timestamp is not None and min_timestamp <= 0:
        errors.append("summary.updateStats.minTimestamp must be positive")
    if max_timestamp is not None and max_timestamp <= 0:
        errors.append("summary.updateStats.maxTimestamp must be positive")
    if min_timestamp is not None and max_timestamp is not None and min_timestamp > max_timestamp:
        errors.append("summary.updateStats.minTimestamp cannot exceed maxTimestamp")

    events_root_value = summary.get("eventStatsSubTreeRoot", summary.get("eventsSubTreeRoot"))
    events_subtree_root = _bytes32_hex(events_root_value, "summary.eventStatsSubTreeRoot", errors)
    subtree_proof = _proof_nodes(payload.get("subTreeProof"), "subTreeProof", errors)
    main_tree_proof = _proof_nodes(payload.get("mainTreeProof"), "mainTreeProof", errors)

    stat_to_prove = payload.get("statToProve")
    if stat_to_prove is None:
        errors.append("statToProve is required")
    event_stat_root = _bytes32_hex(payload.get("eventStatRoot"), "eventStatRoot", errors)
    stat_proof = _proof_nodes(payload.get("statProof"), "statProof", errors)

    second_stat_present = any(
        key in payload for key in ("statToProve2", "eventStatRoot2", "statProof2")
    )
    second_stat: dict[str, Any] | None = None
    if second_stat_present:
        if payload.get("statToProve2") is None:
            errors.append("statToProve2 is required when second-stat fields are present")
        second_stat = {
            "stat_to_prove": payload.get("statToProve2"),
            "event_stat_root_hex": _bytes32_hex(payload.get("eventStatRoot2"), "eventStatRoot2", errors),
            "stat_proof": _proof_nodes(payload.get("statProof2"), "statProof2", errors),
        }

    epoch_day: int | None = None
    epoch_day_le_u16_hex: str | None = None
    if min_timestamp is not None and min_timestamp > 0:
        epoch_day = min_timestamp // DAY_MILLISECONDS
        if not 0 <= epoch_day <= 0xFFFF:
            errors.append("derived epoch day does not fit the documented little-endian u16 PDA seed")
        else:
            epoch_day_le_u16_hex = epoch_day.to_bytes(2, "little").hex()

    if not subtree_proof:
        warnings.append("subTreeProof is empty")
    if not main_tree_proof:
        warnings.append("mainTreeProof is empty")
    if not stat_proof:
        warnings.append("statProof is empty")

    normalized = {
        "fixture_id": str(fixture_id) if fixture_id not in (None, "") else None,
        "update_stats": {
            "update_count": update_count,
            "min_timestamp": min_timestamp,
            "max_timestamp": max_timestamp,
        },
        "events_subtree_root_hex": events_subtree_root,
        "subtree_proof": subtree_proof,
        "main_tree_proof": main_tree_proof,
        "stat": {
            "stat_to_prove": stat_to_prove,
            "event_stat_root_hex": event_stat_root,
            "stat_proof": stat_proof,
        },
        "second_stat": second_stat,
        "daily_scores_pda_seeds": {
            "literal_utf8": DAILY_SCORES_SEED.decode("ascii"),
            "literal_hex": DAILY_SCORES_SEED.hex(),
            "epoch_day": epoch_day,
            "epoch_day_le_u16_hex": epoch_day_le_u16_hex,
        },
    }
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "warnings": warnings,
        "normalized": normalized,
        "structural_fingerprint_sha256": _canonical_sha256(normalized),
        "onchain_view_executed": False,
        "claim_boundary": "strict official-payload structure inspection and PDA seed derivation only; complete validation requires the Solana validateStat view call",
    }
