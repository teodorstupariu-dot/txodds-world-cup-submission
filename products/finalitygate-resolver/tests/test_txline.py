from __future__ import annotations

from finalitygate import TxLineClient, TxLineConfig


def test_https_is_required_outside_local_hosts() -> None:
    try:
        TxLineConfig(origin="http://example.com").validate()
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("insecure remote origin must fail")


def test_local_http_is_allowed_for_test_servers() -> None:
    TxLineConfig(origin="http://127.0.0.1:8000").validate()


def test_origin_cannot_embed_credentials() -> None:
    try:
        TxLineConfig(origin="https://user:password@example.com").validate()
    except ValueError as exc:
        assert "credentials" in str(exc)
    else:
        raise AssertionError("credential-bearing origin must fail")


def test_error_detail_redacts_guest_and_api_tokens() -> None:
    client = TxLineClient(
        TxLineConfig(
            origin="https://example.com",
            guest_jwt="guest-secret-token",
            api_token="api-secret-token",
        )
    )

    detail = client._safe_detail("guest-secret-token api-secret-token")

    assert detail == "<redacted> <redacted>"


def test_path_identifier_is_quoted() -> None:
    assert TxLineClient._segment("fixture/a b") == "fixture%2Fa%20b"


def test_score_validation_requires_nonempty_fixture() -> None:
    client = TxLineClient(TxLineConfig(origin="https://example.com"))
    try:
        client.score_stat_validation(fixture_id="", seq=1, stat_key=1002)
    except ValueError as exc:
        assert "fixture_id" in str(exc)
    else:
        raise AssertionError("empty fixture must fail before network access")


def test_score_validation_rejects_negative_sequence() -> None:
    client = TxLineClient(TxLineConfig(origin="https://example.com"))
    try:
        client.score_stat_validation(fixture_id="fixture-1", seq=-1, stat_key=1002)
    except ValueError as exc:
        assert "seq" in str(exc)
    else:
        raise AssertionError("negative sequence must fail before network access")


def test_config_status_exposes_presence_not_secret_values() -> None:
    config = TxLineConfig(
        origin="https://example.com",
        guest_jwt="guest-secret-token",
        api_token="api-secret-token",
    )

    status = config.status()

    assert status["guest_jwt_present"] is True
    assert status["api_token_present"] is True
    assert "guest-secret-token" not in str(status)
    assert "api-secret-token" not in str(status)
