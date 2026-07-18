from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class TxLineError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TxLineConfig:
    origin: str = "https://txline.txodds.com"
    guest_jwt: str | None = None
    api_token: str | None = None
    timeout: float = 30.0
    retries: int = 2
    backoff_seconds: float = 0.5
    max_response_bytes: int = 10_000_000

    @classmethod
    def from_env(cls) -> "TxLineConfig":
        network = os.getenv("TXLINE_NETWORK", "mainnet").strip().lower()
        default_origin = "https://txline-dev.txodds.com" if network == "devnet" else "https://txline.txodds.com"
        return cls(
            origin=os.getenv("TXLINE_ORIGIN", default_origin).rstrip("/"),
            guest_jwt=os.getenv("TXLINE_GUEST_JWT"),
            api_token=os.getenv("TXLINE_API_TOKEN"),
            timeout=float(os.getenv("TXLINE_TIMEOUT", "30")),
            retries=int(os.getenv("TXLINE_RETRIES", "2")),
            backoff_seconds=float(os.getenv("TXLINE_BACKOFF_SECONDS", "0.5")),
            max_response_bytes=int(os.getenv("TXLINE_MAX_RESPONSE_BYTES", "10000000")),
        )

    def validate(self) -> None:
        parsed = urllib.parse.urlsplit(self.origin)
        if parsed.username or parsed.password:
            raise ValueError("TxLINE origin must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("TxLINE origin must not contain query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("TxLINE origin must not contain an API path")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("TxLINE origin must use HTTPS except for local test servers")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 0 <= self.retries <= 10:
            raise ValueError("retries must be between 0 and 10")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        if self.max_response_bytes < 1024:
            raise ValueError("max_response_bytes must be at least 1024")

    def status(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "guest_jwt_present": bool(self.guest_jwt),
            "api_token_present": bool(self.api_token),
            "timeout": self.timeout,
            "retries": self.retries,
            "max_response_bytes": self.max_response_bytes,
        }


class TxLineClient:
    def __init__(self, config: TxLineConfig | None = None) -> None:
        self.config = config or TxLineConfig.from_env()
        self.config.validate()

    def fixtures_snapshot(self, *, competition_id: int | None = None) -> Any:
        return self._json("/api/fixtures/snapshot", query={"competitionId": competition_id})

    def odds_snapshot(self, fixture_id: str | int) -> Any:
        return self._json(f"/api/odds/snapshot/{self._segment(fixture_id)}")

    def scores_snapshot(self, fixture_id: str | int) -> Any:
        return self._json(f"/api/scores/snapshot/{self._segment(fixture_id)}")

    def odds_validation(self, *, message_id: str, ts: int) -> Any:
        clean_message_id = str(message_id).strip()
        if not clean_message_id:
            raise ValueError("message_id is required")
        if int(ts) <= 0:
            raise ValueError("ts must be a positive Unix timestamp in milliseconds")
        return self._json("/api/odds/validation", query={"messageId": clean_message_id, "ts": int(ts)})

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "proofguard-autonomous-agent/0.1"}
        if self.config.guest_jwt:
            headers["Authorization"] = f"Bearer {self.config.guest_jwt}"
        if self.config.api_token:
            headers["X-Api-Token"] = self.config.api_token
        return headers

    def _safe_detail(self, detail: str) -> str:
        sanitized = detail
        for secret in (self.config.guest_jwt, self.config.api_token):
            if secret:
                sanitized = sanitized.replace(secret, "<redacted>")
        return sanitized[:500]

    def _json(self, path: str, *, query: dict[str, Any] | None = None) -> Any:
        url = self.config.origin + path
        if query:
            clean = {key: value for key, value in query.items() if value is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        request = urllib.request.Request(url, headers=self._headers())
        with self._open(request) as response:
            raw = self._read_limited(response)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TxLineError("TxLINE returned invalid UTF-8 JSON") from exc

    def _open(self, request: urllib.request.Request) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=self.config.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                if not retryable or attempt >= self.config.retries:
                    detail = self._safe_detail(exc.read().decode("utf-8", "replace"))
                    raise TxLineError(f"TxLINE HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt >= self.config.retries:
                    raise TxLineError(f"TxLINE unavailable: {self._safe_detail(str(exc.reason))}") from exc
            if self.config.backoff_seconds:
                time.sleep(self.config.backoff_seconds * (2**attempt))
        raise TxLineError(f"TxLINE request failed: {last_error}")

    def _read_limited(self, response: Any) -> bytes:
        try:
            raw = response.read(self.config.max_response_bytes + 1)
        except TypeError:
            raw = response.read()
        if len(raw) > self.config.max_response_bytes:
            raise TxLineError(f"TxLINE response exceeded {self.config.max_response_bytes} bytes")
        return raw

    @staticmethod
    def _segment(value: str | int) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("path identifier must not be empty")
        return urllib.parse.quote(text, safe="-_.~")
