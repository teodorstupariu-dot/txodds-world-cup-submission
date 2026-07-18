from __future__ import annotations

from datetime import datetime, timezone

from proofguard_agent.adapter import apply_score_finality, events_from_odds_payload
from proofguard_agent.txline import TxLineClient, TxLineConfig

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _odds_events():
    return events_from_odds_payload(
        {
            "data": [
                {
                    "fixtureId": "fixture-1",
                    "market": "MATCH_RESULT",
                    "selection": "HOME",
                    "stablePrice": 0.45,
                    "timestamp": "2026-07-08T11:59:30Z",
                    "proofRef": "proof-1",
                    "messageId": "message-home",
                },
                {
                    "fixtureId": "fixture-1",
                    "market": "MATCH_RESULT",
                    "selection": "DRAW",
                    "stablePrice": 0.30,
                    "timestamp": "2026-07-08T11:59:30Z",
                    "proofRef": "proof-1",
                    "messageId": "message-draw",
                },
                {
                    "fixtureId": "fixture-1",
                    "market": "MATCH_RESULT",
                    "selection": "AWAY",
                    "stablePrice": 0.25,
                    "timestamp": "2026-07-08T11:59:30Z",
                    "proofRef": "proof-1",
                    "messageId": "message-away",
                },
            ]
        },
        fixture_id="fixture-1",
        model_probabilities={"HOME": 0.60, "DRAW": 0.25, "AWAY": 0.15},
        observed_at=NOW,
    )


def test_odds_payload_normalizes_without_persisting_raw_data() -> None:
    events = _odds_events()

    assert len(events) == 3
    assert {event.selection for event in events} == {"HOME", "DRAW", "AWAY"}
    assert all(event.market_probability_sum == 1.0 for event in events)
    assert all(event.stale_seconds == 30.0 for event in events)
    assert all(event.proof_ready for event in events)
    assert all(event.source_fingerprint and len(event.source_fingerprint) == 64 for event in events)


def test_devig_removes_bookmaker_margin() -> None:
    # A vigged 3-way book (implied probabilities sum to 1.10 = 10% overround).
    payload = {
        "data": [
            {"fixtureId": "fixture-1", "market": "MATCH_RESULT", "selection": "HOME", "probability": 0.50, "timestamp": "2026-07-08T11:59:30Z"},
            {"fixtureId": "fixture-1", "market": "MATCH_RESULT", "selection": "DRAW", "probability": 0.33, "timestamp": "2026-07-08T11:59:30Z"},
            {"fixtureId": "fixture-1", "market": "MATCH_RESULT", "selection": "AWAY", "probability": 0.27, "timestamp": "2026-07-08T11:59:30Z"},
        ]
    }
    events = events_from_odds_payload(payload, fixture_id="fixture-1", model_probabilities={}, observed_at=NOW)

    for event in events:
        assert event.market_probability_sum == 1.10
        assert round(event.overround, 2) == 0.10
        # Fair probability is the vig-free normalization and is < the raw implied.
        assert event.fair_probability == round(event.market_probability / 1.10, 9) or abs(event.fair_probability - event.market_probability / 1.10) < 1e-6
        assert event.fair_probability < event.market_probability
        assert event.effective_market_probability == event.fair_probability
    # De-vigged probabilities form a coherent distribution.
    assert abs(sum(event.fair_probability for event in events) - 1.0) < 1e-6


def test_decimal_odds_are_converted_to_probability() -> None:
    payload = [{
        "fixtureId": "fixture-1",
        "market": "MATCH_RESULT",
        "selection": "HOME",
        "decimalOdds": 2.0,
        "timestamp": "2026-07-08T12:00:00Z",
    }]
    event = events_from_odds_payload(payload, fixture_id="fixture-1", model_probabilities={}, observed_at=NOW)[0]
    assert event.market_probability == 0.5
    assert event.model_probability == 0.5
    assert event.proof_ready is False


def test_future_timestamp_is_marked_backwards() -> None:
    payload = [{
        "fixtureId": "fixture-1",
        "market": "MATCH_RESULT",
        "selection": "HOME",
        "probability": 0.5,
        "timestamp": "2026-07-08T12:01:00Z",
    }]
    event = events_from_odds_payload(payload, fixture_id="fixture-1", model_probabilities={}, observed_at=NOW)[0]
    assert event.backwards_timestamp is True
    assert event.stale_seconds == 0.0


def test_final_score_marks_all_fixture_events_final_and_derives_home_win() -> None:
    updated, summary = apply_score_finality(
        _odds_events(),
        {
            "scores": [
                {
                    "fixtureId": "fixture-1",
                    "gameState": "FT",
                    "homeScore": 2,
                    "awayScore": 1,
                    "timestamp": "2026-07-08T12:00:00Z",
                }
            ]
        },
        fixture_id="fixture-1",
    )

    assert summary["fixture_final"] is True
    assert summary["winning_selection"] == "HOME"
    assert summary["home_score"] == 2
    assert summary["away_score"] == 1
    assert len(summary["schema_fingerprint"]) == 64
    assert all(event.fixture_final for event in updated)
    assert all(event.winning_selection == "HOME" for event in updated)


def test_nonfinal_score_does_not_close_fixture() -> None:
    updated, summary = apply_score_finality(
        _odds_events(),
        [{"fixtureId": "fixture-1", "status": "LIVE", "score1": 1, "score2": 1}],
        fixture_id="fixture-1",
    )

    assert summary["fixture_final"] is False
    assert summary["winning_selection"] is None
    assert all(not event.fixture_final for event in updated)


def test_missing_score_rows_preserve_events() -> None:
    events = _odds_events()
    updated, summary = apply_score_finality(events, {"scores": []}, fixture_id="fixture-1")
    assert updated == events
    assert summary["score_rows"] == 0
    assert summary["fixture_final"] is False


def test_https_is_required_outside_local_test_hosts() -> None:
    try:
        TxLineConfig(origin="http://example.com").validate()
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("insecure remote origin must fail")


def test_local_http_origin_is_allowed_for_tests() -> None:
    TxLineConfig(origin="http://127.0.0.1:8000").validate()


def test_error_detail_redacts_both_credentials() -> None:
    client = TxLineClient(TxLineConfig(origin="https://example.com", guest_jwt="guest-secret", api_token="api-secret"))
    sanitized = client._safe_detail("guest-secret api-secret")
    assert "guest-secret" not in sanitized
    assert "api-secret" not in sanitized
    assert sanitized == "<redacted> <redacted>"


def test_path_segment_is_quoted() -> None:
    assert TxLineClient._segment("fixture/a b") == "fixture%2Fa%20b"
