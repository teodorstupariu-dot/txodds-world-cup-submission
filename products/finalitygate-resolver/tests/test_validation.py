from __future__ import annotations

import base64

from finalitygate import inspect_score_stat_validation

ROOT = bytes(range(32))
ROOT_HEX = ROOT.hex()
ROOT_BASE64 = base64.b64encode(ROOT).decode("ascii")


def node(*, right: bool, encoding: str = "hex") -> dict[str, object]:
    if encoding == "hex":
        value: object = ROOT_HEX
    elif encoding == "base64":
        value = ROOT_BASE64
    else:
        value = list(ROOT)
    return {"hash": value, "isRightSibling": right}


def valid_payload() -> dict[str, object]:
    return {
        "summary": {
            "fixtureId": 17952170,
            "updateStats": {
                "updateCount": 12,
                "minTimestamp": 1783472400000,
                "maxTimestamp": 1783472700000,
            },
            "eventStatsSubTreeRoot": ROOT_BASE64,
        },
        "subTreeProof": [node(right=True)],
        "mainTreeProof": [node(right=False, encoding="base64")],
        "statToProve": {"statKey": 1002, "value": 2},
        "eventStatRoot": list(ROOT),
        "statProof": [node(right=True, encoding="list")],
    }


def test_valid_official_shape_passes_and_derives_pda_seed() -> None:
    result = inspect_score_stat_validation(valid_payload())

    assert result["status"] == "PASS"
    assert result["errors"] == []
    assert result["normalized"]["fixture_id"] == "17952170"
    assert result["normalized"]["events_subtree_root_hex"] == ROOT_HEX
    assert result["normalized"]["daily_scores_pda_seeds"]["literal_utf8"] == "daily_scores_roots"
    epoch_day = 1783472400000 // 86_400_000
    assert result["normalized"]["daily_scores_pda_seeds"]["epoch_day"] == epoch_day
    assert result["normalized"]["daily_scores_pda_seeds"]["epoch_day_le_u16_hex"] == epoch_day.to_bytes(2, "little").hex()
    assert len(result["structural_fingerprint_sha256"]) == 64
    assert result["onchain_view_executed"] is False


def test_signed_byte_arrays_match_unsigned_bytes() -> None:
    signed = [value if value < 128 else value - 256 for value in range(224, 256)]
    payload = valid_payload()
    payload["eventStatRoot"] = signed
    payload["summary"]["eventStatsSubTreeRoot"] = signed  # type: ignore[index]

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "PASS"
    expected = bytes(range(224, 256)).hex()
    assert result["normalized"]["events_subtree_root_hex"] == expected
    assert result["normalized"]["stat"]["event_stat_root_hex"] == expected


def test_second_stat_shape_is_supported() -> None:
    payload = valid_payload()
    payload.update({
        "statToProve2": {"statKey": 1003, "value": 1},
        "eventStatRoot2": "0x" + ROOT_HEX,
        "statProof2": [node(right=False)],
    })

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "PASS"
    assert result["normalized"]["second_stat"]["event_stat_root_hex"] == ROOT_HEX


def test_missing_summary_fails_closed() -> None:
    result = inspect_score_stat_validation({})

    assert result["status"] == "FAIL"
    assert "summary must be an object" in result["errors"]
    assert result["onchain_view_executed"] is False


def test_invalid_hash_length_fails() -> None:
    payload = valid_payload()
    payload["eventStatRoot"] = "00" * 31

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "FAIL"
    assert any("eventStatRoot must contain exactly 32 bytes" in error for error in result["errors"])


def test_invalid_proof_direction_fails() -> None:
    payload = valid_payload()
    payload["statProof"] = [{"hash": ROOT_HEX, "isRightSibling": "yes"}]

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "FAIL"
    assert any("statProof[0].isRightSibling must be boolean" in error for error in result["errors"])


def test_timestamp_order_conflict_fails() -> None:
    payload = valid_payload()
    payload["summary"]["updateStats"]["minTimestamp"] = 2000  # type: ignore[index]
    payload["summary"]["updateStats"]["maxTimestamp"] = 1000  # type: ignore[index]

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "FAIL"
    assert "summary.updateStats.minTimestamp cannot exceed maxTimestamp" in result["errors"]


def test_empty_proofs_warn_but_preserve_structural_pass() -> None:
    payload = valid_payload()
    payload["subTreeProof"] = []
    payload["mainTreeProof"] = []
    payload["statProof"] = []

    result = inspect_score_stat_validation(payload)

    assert result["status"] == "PASS"
    assert set(result["warnings"]) == {
        "subTreeProof is empty",
        "mainTreeProof is empty",
        "statProof is empty",
    }


def test_non_object_payload_fails() -> None:
    result = inspect_score_stat_validation([])
    assert result["status"] == "FAIL"
    assert result["errors"] == ["validation payload must be an object"]
