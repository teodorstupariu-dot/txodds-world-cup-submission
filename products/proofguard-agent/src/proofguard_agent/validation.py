from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any

DAY_MILLISECONDS = 24 * 60 * 60 * 1000
DAILY_ODDS_SEED = b"daily_batch_roots"


def _integer(value: Any, field: str, errors: list[str]) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer")
        return None


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


def inspect_odds_validation(payload: Any) -> dict[str, Any]:
    """Inspect the documented TxODDS ``/api/odds/validation`` shape.

    The function validates the odds record, fixture summary, proof-node shapes,
    and documented daily-odds PDA seed inputs. It does not serialize the record
    into the exact on-chain leaf or execute ``validateOdds``.
    """

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return {
            "status": "FAIL",
            "errors": ["validation payload must be an object"],
            "warnings": [],
            "claim_boundary": "structural inspection only; no validateOdds execution",
        }

    odds = payload.get("odds")
    if not isinstance(odds, dict):
        errors.append("odds must be an object")
        odds = {}
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary must be an object")
        summary = {}
    update_stats = summary.get("updateStats")
    if not isinstance(update_stats, dict):
        errors.append("summary.updateStats must be an object")
        update_stats = {}

    fixture_id = odds.get("FixtureId")
    summary_fixture_id = summary.get("fixtureId")
    if fixture_id in (None, ""):
        errors.append("odds.FixtureId is required")
    if summary_fixture_id in (None, ""):
        errors.append("summary.fixtureId is required")
    if fixture_id not in (None, "") and summary_fixture_id not in (None, "") and str(fixture_id) != str(summary_fixture_id):
        errors.append("odds.FixtureId must match summary.fixtureId")

    message_id = odds.get("MessageId")
    if not isinstance(message_id, str) or not message_id.strip():
        errors.append("odds.MessageId must be a non-empty string")

    timestamp = _integer(odds.get("Ts"), "odds.Ts", errors)
    if timestamp is not None and timestamp <= 0:
        errors.append("odds.Ts must be positive")

    required_scalar_fields = ("Bookmaker", "BookmakerId", "SuperOddsType", "InRunning")
    for field in required_scalar_fields:
        if odds.get(field) is None:
            errors.append(f"odds.{field} is required")

    price_names = odds.get("PriceNames")
    prices = odds.get("Prices")
    if not isinstance(price_names, list) or not price_names:
        errors.append("odds.PriceNames must be a non-empty list")
        price_names = []
    if not isinstance(prices, list) or not prices:
        errors.append("odds.Prices must be a non-empty list")
        prices = []
    if price_names and prices and len(price_names) != len(prices):
        errors.append("odds.PriceNames and odds.Prices must have the same length")

    update_count = _integer(update_stats.get("updateCount"), "summary.updateStats.updateCount", errors)
    min_timestamp = _integer(update_stats.get("minTimestamp"), "summary.updateStats.minTimestamp", errors)
    max_timestamp = _integer(update_stats.get("maxTimestamp"), "summary.updateStats.maxTimestamp", errors)
    if update_count is not None and update_count < 0:
        errors.append("summary.updateStats.updateCount cannot be negative")
    if min_timestamp is not None and max_timestamp is not None and min_timestamp > max_timestamp:
        errors.append("summary.updateStats.minTimestamp cannot exceed maxTimestamp")
    if timestamp is not None and min_timestamp is not None and max_timestamp is not None and not min_timestamp <= timestamp <= max_timestamp:
        warnings.append("odds.Ts falls outside summary update timestamp bounds")

    odds_subtree_root = _bytes32_hex(summary.get("oddsSubTreeRoot"), "summary.oddsSubTreeRoot", errors)
    subtree_proof = _proof_nodes(payload.get("subTreeProof"), "subTreeProof", errors)
    main_tree_proof = _proof_nodes(payload.get("mainTreeProof"), "mainTreeProof", errors)
    if not subtree_proof:
        warnings.append("subTreeProof is empty")
    if not main_tree_proof:
        warnings.append("mainTreeProof is empty")

    epoch_day: int | None = None
    epoch_day_le_u16_hex: str | None = None
    if timestamp is not None and timestamp > 0:
        epoch_day = timestamp // DAY_MILLISECONDS
        if not 0 <= epoch_day <= 0xFFFF:
            errors.append("derived epoch day does not fit the documented little-endian u16 PDA seed")
        else:
            epoch_day_le_u16_hex = epoch_day.to_bytes(2, "little").hex()

    normalized = {
        "odds": {
            "fixture_id": str(fixture_id) if fixture_id not in (None, "") else None,
            "message_id": message_id.strip() if isinstance(message_id, str) else None,
            "timestamp": timestamp,
            "bookmaker": odds.get("Bookmaker"),
            "bookmaker_id": odds.get("BookmakerId"),
            "super_odds_type": odds.get("SuperOddsType"),
            "game_state": odds.get("GameState"),
            "in_running": odds.get("InRunning"),
            "market_parameters": odds.get("MarketParameters"),
            "market_period": odds.get("MarketPeriod"),
            "price_names": price_names,
            "prices": prices,
        },
        "summary": {
            "fixture_id": str(summary_fixture_id) if summary_fixture_id not in (None, "") else None,
            "update_count": update_count,
            "min_timestamp": min_timestamp,
            "max_timestamp": max_timestamp,
            "odds_subtree_root_hex": odds_subtree_root,
        },
        "subtree_proof": subtree_proof,
        "main_tree_proof": main_tree_proof,
        "daily_odds_pda_seeds": {
            "literal_utf8": DAILY_ODDS_SEED.decode("ascii"),
            "literal_hex": DAILY_ODDS_SEED.hex(),
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
        "proof_reference": {
            "message_id": normalized["odds"]["message_id"],
            "timestamp": timestamp,
        },
        "exact_leaf_serialization_executed": False,
        "onchain_validate_odds_executed": False,
        "claim_boundary": "strict official odds-validation payload inspection and PDA seed derivation only; complete verification requires exact record serialization and the Solana validateOdds call",
    }
