from __future__ import annotations

from proofguard_agent import inspect_odds_validation


def valid_payload() -> dict[str, object]:
    return {
        "odds": {
            "FixtureId": 1814961080,
            "MessageId": "1814961080:00003:000084-10011-stab",
            "Ts": 1770845011255,
            "Bookmaker": "ExampleBook",
            "BookmakerId": 3,
            "SuperOddsType": "MATCH_RESULT",
            "GameState": "LIVE",
            "InRunning": True,
            "MarketParameters": None,
            "MarketPeriod": "FULL_TIME",
            "PriceNames": ["HOME", "DRAW", "AWAY"],
            "Prices": [0.45, 0.30, 0.25],
        },
        "summary": {
            "fixtureId": 1814961080,
            "updateStats": {
                "updateCount": 84,
                "minTimestamp": 1770845000000,
                "maxTimestamp": 1770845020000,
            },
            "oddsSubTreeRoot": [
                -1, -2, -3, -4, -5, -6, -7, -8,
                -9, -10, -11, -12, -13, -14, -15, -16,
                0, 1, 2, 3, 4, 5, 6, 7,
                8, 9, 10, 11, 12, 13, 14, 15,
            ],
        },
        "subTreeProof": [{"hash": "11" * 32, "isRightSibling": True}],
        "mainTreeProof": [{"hash": "22" * 32, "isRightSibling": False}],
    }


def test_valid_official_odds_shape_passes() -> None:
    result = inspect_odds_validation(valid_payload())

    assert result["status"] == "PASS"
    assert result["errors"] == []
    assert result["normalized"]["odds"]["message_id"] == "1814961080:00003:000084-10011-stab"
    assert result["normalized"]["summary"]["fixture_id"] == "1814961080"
    assert result["normalized"]["daily_odds_pda_seeds"]["literal_utf8"] == "daily_batch_roots"
    epoch_day = 1770845011255 // 86_400_000
    assert result["normalized"]["daily_odds_pda_seeds"]["epoch_day"] == epoch_day
    assert result["normalized"]["daily_odds_pda_seeds"]["epoch_day_le_u16_hex"] == epoch_day.to_bytes(2, "little").hex()
    assert len(result["structural_fingerprint_sha256"]) == 64
    assert result["exact_leaf_serialization_executed"] is False
    assert result["onchain_validate_odds_executed"] is False


def test_signed_subtree_root_bytes_are_normalized() -> None:
    result = inspect_odds_validation(valid_payload())
    expected = bytes([
        255, 254, 253, 252, 251, 250, 249, 248,
        247, 246, 245, 244, 243, 242, 241, 240,
        0, 1, 2, 3, 4, 5, 6, 7,
        8, 9, 10, 11, 12, 13, 14, 15,
    ]).hex()
    assert result["normalized"]["summary"]["odds_subtree_root_hex"] == expected


def test_fixture_mismatch_fails_closed() -> None:
    payload = valid_payload()
    payload["summary"]["fixtureId"] = 999  # type: ignore[index]

    result = inspect_odds_validation(payload)

    assert result["status"] == "FAIL"
    assert "odds.FixtureId must match summary.fixtureId" in result["errors"]


def test_price_name_and_value_length_mismatch_fails() -> None:
    payload = valid_payload()
    payload["odds"]["Prices"] = [0.45, 0.55]  # type: ignore[index]

    result = inspect_odds_validation(payload)

    assert result["status"] == "FAIL"
    assert "odds.PriceNames and odds.Prices must have the same length" in result["errors"]


def test_missing_message_id_fails() -> None:
    payload = valid_payload()
    payload["odds"]["MessageId"] = ""  # type: ignore[index]

    result = inspect_odds_validation(payload)

    assert result["status"] == "FAIL"
    assert "odds.MessageId must be a non-empty string" in result["errors"]


def test_timestamp_outside_summary_bounds_warns() -> None:
    payload = valid_payload()
    payload["odds"]["Ts"] = 1770846000000  # type: ignore[index]

    result = inspect_odds_validation(payload)

    assert result["status"] == "PASS"
    assert "odds.Ts falls outside summary update timestamp bounds" in result["warnings"]


def test_invalid_proof_hash_fails() -> None:
    payload = valid_payload()
    payload["subTreeProof"] = [{"hash": "00" * 31, "isRightSibling": True}]

    result = inspect_odds_validation(payload)

    assert result["status"] == "FAIL"
    assert any("subTreeProof[0].hash must contain exactly 32 bytes" in error for error in result["errors"])


def test_non_object_payload_fails() -> None:
    result = inspect_odds_validation([])
    assert result["status"] == "FAIL"
    assert result["errors"] == ["validation payload must be an object"]
