from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Iterable

from .core import MarketEvent

FINAL_SCORE_STATES = {"FINAL", "FINISHED", "FT", "AET", "PEN", "F", "FO"}


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return default


def _rows(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        for row in payload:
            if isinstance(row, dict):
                yield row
        return
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "odds", "scores", "fixtures", "updates"):
            value = payload.get(key)
            if isinstance(value, list):
                yield from _rows(value)
                return
        yield payload


def _probability(raw: dict[str, Any]) -> float | None:
    value = _first(raw, "Probability", "probability", "StablePrice", "stablePrice", "price", "value", "impliedProbability")
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = -1.0
    if 1.0 < number <= 100.0:
        number /= 100.0
    if 0.0 < number < 1.0:
        return number
    try:
        decimal_odds = float(_first(raw, "DecimalOdds", "decimalOdds", "odds"))
    except (TypeError, ValueError):
        return None
    return 1.0 / decimal_odds if decimal_odds > 1.0 else None


def _devig(implied: float, book_sum: float) -> float | None:
    """Proportional (multiplicative) de-vig: fair = implied / sum(implied).

    Removes the bookmaker margin so the agent measures edge against a
    normalized, vig-free probability. Returns None when the book sum is not
    usable; clamps strictly inside (0, 1) to satisfy MarketEvent validation.
    """
    if book_sum <= 0:
        return None
    fair = implied / book_sum
    return min(max(fair, 1e-9), 1.0 - 1e-9)


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _timestamp(raw: dict[str, Any], fallback: datetime) -> datetime:
    value = _first(raw, "timestamp", "ts", "updatedAt", "UpdateTime", "createdAt", "time")
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return datetime.fromtimestamp(number, tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return fallback


def _schema_fingerprint(rows: list[dict[str, Any]]) -> str:
    schema = sorted({str(key) for row in rows for key in row})
    return hashlib.sha256(json.dumps(schema, separators=(",", ":")).encode("utf-8")).hexdigest()


def events_from_odds_payload(
    payload: Any,
    *,
    fixture_id: str,
    model_probabilities: dict[str, float],
    observed_at: datetime | None = None,
) -> list[MarketEvent]:
    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    source_rows = list(_rows(payload))
    fingerprint = _schema_fingerprint(source_rows)
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(source_rows):
        current_fixture = str(_first(raw, "FixtureId", "fixtureId", "fixture_id", default=fixture_id)).strip()
        if current_fixture != fixture_id:
            continue
        probability = _probability(raw)
        if probability is None:
            continue
        market = str(_first(raw, "Market", "market", "MarketName", "marketName", "marketType", default="MATCH_RESULT")).strip()
        selection = str(_first(raw, "Selection", "selection", "SelectionName", "selectionName", "outcome", "participant", default="UNKNOWN")).strip().upper()
        timestamp = _timestamp(raw, now)
        proof_reference = str(_first(raw, "proof", "proofRef", "merkleRoot", "batchHash", default="")).strip() or None
        model_key = f"{market}|{selection}"
        model_probability = model_probabilities.get(model_key, model_probabilities.get(selection, probability))
        normalized.append({
            "event_id": str(_first(raw, "messageId", "MessageId", "id", "seq", "sequence", default=f"snapshot-{index}")),
            "fixture_id": current_fixture,
            "market": market,
            "selection": selection,
            "market_probability": probability,
            "model_probability": float(model_probability),
            "timestamp": timestamp,
            "proof_ready": proof_reference is not None,
        })
    if not normalized:
        raise ValueError("TxLINE odds payload produced no valid ProofGuard events")

    sums: dict[str, float] = {}
    for row in normalized:
        sums[row["market"]] = sums.get(row["market"], 0.0) + row["market_probability"]

    events = [
        MarketEvent(
            event_id=row["event_id"],
            fixture_id=row["fixture_id"],
            market=row["market"],
            selection=row["selection"],
            market_probability=row["market_probability"],
            model_probability=row["model_probability"],
            market_probability_sum=sums[row["market"]],
            fair_probability=_devig(row["market_probability"], sums[row["market"]]),
            stale_seconds=max(0.0, (now - row["timestamp"].astimezone(timezone.utc)).total_seconds()),
            proof_ready=row["proof_ready"],
            backwards_timestamp=row["timestamp"] > now,
            observed_at=now,
            source_fingerprint=fingerprint,
        )
        for row in normalized
    ]
    return sorted(events, key=lambda item: (item.market, item.selection, item.event_id))


def apply_score_finality(
    events: list[MarketEvent],
    scores_payload: Any,
    *,
    fixture_id: str,
) -> tuple[list[MarketEvent], dict[str, Any]]:
    score_rows = [
        row
        for row in _rows(scores_payload)
        if str(_first(row, "FixtureId", "fixtureId", "fixture_id", default=fixture_id)).strip() == fixture_id
    ]
    if not score_rows:
        return events, {
            "score_rows": 0,
            "fixture_final": False,
            "winning_selection": None,
            "schema_fingerprint": _schema_fingerprint([]),
        }

    latest = max(score_rows, key=lambda row: _timestamp(row, datetime.min.replace(tzinfo=timezone.utc)))
    state = str(_first(latest, "gameState", "GameState", "status", "Status", "phase", default="")).strip().upper()
    home = _integer(_first(latest, "homeScore", "HomeScore", "participant1Score", "Participant1Score", "score1"))
    away = _integer(_first(latest, "awayScore", "AwayScore", "participant2Score", "Participant2Score", "score2"))
    fixture_final = state in FINAL_SCORE_STATES
    winner: str | None = None
    if fixture_final and home is not None and away is not None:
        winner = "HOME" if home > away else "AWAY" if away > home else "DRAW"

    updated = [
        replace(event, fixture_final=fixture_final, winning_selection=winner)
        for event in events
    ]
    return updated, {
        "score_rows": len(score_rows),
        "fixture_final": fixture_final,
        "winning_selection": winner,
        "game_state": state or None,
        "home_score": home,
        "away_score": away,
        "schema_fingerprint": _schema_fingerprint(score_rows),
    }
