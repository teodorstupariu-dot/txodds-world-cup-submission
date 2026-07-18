from __future__ import annotations

import asyncio
import copy
import json
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from dataclasses import replace

from ..adapter import apply_score_finality, events_from_odds_payload
from ..core import MarketEvent, ProofGuardAutonomousAgent
from ..model import match_result_probabilities
from ..txline import TxLineClient, TxLineConfig

RuntimeMode = Literal["AUTO", "LIVE", "REPLAY"]
SourceMode = Literal["LIVE", "REPLAY", "REPLAY_FALLBACK", "ERROR"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_models() -> tuple[dict[str, float], bool]:
    raw = os.getenv("PROOFGUARD_MODEL_PROBABILITIES_JSON", "").strip()
    model_file = os.getenv("PROOFGUARD_MODEL_PROBABILITIES_FILE", "").strip()
    configured = bool(raw or model_file)
    if model_file:
        raw = Path(model_file).read_text(encoding="utf-8")
    if not raw:
        return {"HOME": 0.58, "DRAW": 0.25, "AWAY": 0.17}, False
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("model probabilities must be a JSON object")
    models: dict[str, float] = {}
    for key, value in payload.items():
        number = float(value)
        if not 0.0 < number < 1.0:
            raise ValueError(f"model probability for {key!r} must be in (0, 1)")
        models[str(key)] = number
    if not models:
        raise ValueError("at least one model probability is required")
    return models, configured


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    requested_mode: RuntimeMode = "AUTO"
    fixture_id: str | None = None
    model_probabilities: dict[str, float] = field(default_factory=lambda: {"HOME": 0.58, "DRAW": 0.25, "AWAY": 0.17})
    models_explicitly_configured: bool = False
    poll_seconds: float = 60.0
    with_scores: bool = True
    replay_fallback: bool = True
    auto_start: bool = True
    max_receipts: int = 100
    txline: TxLineConfig = field(default_factory=TxLineConfig.from_env, repr=False)

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        requested = os.getenv("PROOFGUARD_MODE", "AUTO").strip().upper()
        if requested not in {"AUTO", "LIVE", "REPLAY"}:
            raise ValueError("PROOFGUARD_MODE must be AUTO, LIVE, or REPLAY")
        models, configured = _load_models()
        poll_seconds = float(os.getenv("PROOFGUARD_POLL_SECONDS", "60"))
        if not 0.25 <= poll_seconds <= 3600:
            raise ValueError("PROOFGUARD_POLL_SECONDS must be between 0.25 and 3600")
        max_receipts = int(os.getenv("PROOFGUARD_MAX_RECEIPTS", "100"))
        if not 10 <= max_receipts <= 5000:
            raise ValueError("PROOFGUARD_MAX_RECEIPTS must be between 10 and 5000")
        return cls(
            requested_mode=requested,  # type: ignore[arg-type]
            fixture_id=os.getenv("PROOFGUARD_FIXTURE_ID", "").strip() or None,
            model_probabilities=models,
            models_explicitly_configured=configured,
            poll_seconds=poll_seconds,
            with_scores=_env_bool("PROOFGUARD_WITH_SCORES", True),
            replay_fallback=_env_bool("PROOFGUARD_REPLAY_FALLBACK", True),
            auto_start=_env_bool("PROOFGUARD_AUTO_START", True),
            max_receipts=max_receipts,
            txline=TxLineConfig.from_env(),
        )

    @property
    def missing_live_requirements(self) -> tuple[str, ...]:
        missing: list[str] = []
        if not self.fixture_id:
            missing.append("fixture_id")
        if not self.models_explicitly_configured:
            missing.append("model_probabilities")
        if not self.txline.guest_jwt:
            missing.append("txline_guest_jwt")
        if not self.txline.api_token:
            missing.append("txline_api_token")
        return tuple(missing)

    @property
    def live_ready(self) -> bool:
        return not self.missing_live_requirements

    def public_view(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "fixture_id": self.fixture_id,
            "poll_seconds": self.poll_seconds,
            "with_scores": self.with_scores,
            "replay_fallback": self.replay_fallback,
            "auto_start": self.auto_start,
            "max_receipts": self.max_receipts,
            "models_configured": self.models_explicitly_configured,
            "live_ready": self.live_ready,
            "missing_live_requirements": list(self.missing_live_requirements),
            "txline": self.txline.status(),
        }


# Deterministic scripted "full match" replay. Each public REPLAY cycle advances
# one step of this timeline and loops, so the always-on demo tells one coherent
# 90-minute story (value entry -> drift -> second entry -> stale REVIEW ->
# corrupted-feed BLOCK -> recovery -> late entry -> proof wait -> full-time
# CLOSE) that exercises every action and every integrity verdict in order.
# Step 0 is a clean value ENTER so even a single cold cycle already shows an
# entry. The full-time CLOSE clears all positions, so every loop starts fresh.
REPLAY_SCENARIO: tuple[dict[str, Any], ...] = (
    {"tag": "kickoff-enter", "minute": 1, "selection": "HOME",
     "narrative": "Kickoff. Clean feed, clear model edge on HOME - agent opens a paper position.",
     "market_probability": 0.45, "model_probability": 0.64},
    {"tag": "drift-hold", "minute": 12, "selection": "HOME",
     "narrative": "Market drifts toward fair value; edge too small to act - HOLD.",
     "market_probability": 0.60, "model_probability": 0.63},
    {"tag": "draw-value-enter", "minute": 23, "selection": "DRAW",
     "narrative": "Second value opens on the DRAW; agent opens a paper position within its exposure cap.",
     "market_probability": 0.28, "model_probability": 0.36},
    {"tag": "stale-review-hold", "minute": 34, "selection": "HOME",
     "narrative": "Feed goes stale (>90s). Integrity REVIEW forbids any new entry - HOLD.",
     "market_probability": 0.55, "model_probability": 0.66, "stale_seconds": 120.0},
    {"tag": "corrupt-block-reject", "minute": 45, "selection": "AWAY",
     "narrative": "Corrupted book: backwards timestamp, incoherent sum, no proof. Integrity BLOCK -> REJECT.",
     "market_probability": 0.25, "model_probability": 0.58,
     "probability_sum": 1.16, "stale_seconds": 240.0, "proof_ready": False, "backwards": True},
    {"tag": "recover-hold", "minute": 58, "selection": "HOME",
     "narrative": "Feed recovers and is clean again, but there is no edge to trade - HOLD.",
     "market_probability": 0.62, "model_probability": 0.63},
    {"tag": "late-value-enter", "minute": 70, "selection": "HOME",
     "narrative": "Late value returns on HOME; agent re-enters within its exposure cap.",
     "market_probability": 0.50, "model_probability": 0.61},
    {"tag": "proof-wait-review", "minute": 79, "selection": "HOME",
     "narrative": "Model edge present but on-chain proof not ready. Integrity REVIEW -> HOLD.",
     "market_probability": 0.50, "model_probability": 0.62, "proof_ready": False},
    {"tag": "full-time-close", "minute": 90, "selection": "HOME",
     "narrative": "Full time. Fixture final: settle and close all open paper positions.",
     "market_probability": 0.95, "model_probability": 0.95,
     "fixture_final": True, "winning_selection": "HOME"},
)


class ProofGuardRuntime:
    """Stateful autonomous runtime for the public live dashboard.

    The runtime never persists raw TxLINE payloads. Public state contains only
    normalized events, decisions, positions, bounded receipts, safe identifiers,
    and redacted errors.
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        client_factory: Callable[[TxLineConfig], TxLineClient] = TxLineClient,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.config = config or RuntimeConfig.from_env()
        self.config.txline.validate()
        self._clock = clock
        self._client_factory = client_factory
        self._agent = ProofGuardAutonomousAgent()
        self._cycle_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started_at = self._clock()
        self._last_cycle_started_at: datetime | None = None
        self._last_cycle_finished_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._source_mode: SourceMode = "REPLAY"
        self._cycle_count = 0
        self._consecutive_errors = 0
        self._last_error: str | None = None
        self._latest_events: list[dict[str, Any]] = []
        self._latest_cycle: dict[str, Any] | None = None
        self._latest_source: dict[str, Any] = {}
        self._receipts: deque[dict[str, Any]] = deque(maxlen=self.config.max_receipts)
        self._recent_errors: deque[dict[str, Any]] = deque(maxlen=20)
        self._replay_index = 0

    @property
    def task_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.task_running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self.run_forever(), name="proofguard-runtime")

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=max(2.0, min(10.0, self.config.poll_seconds + 1.0)))
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            await self.cycle_once()
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.config.poll_seconds)
            except asyncio.TimeoutError:
                continue

    async def cycle_once(self) -> dict[str, Any]:
        async with self._cycle_lock:
            started = self._clock()
            async with self._state_lock:
                self._last_cycle_started_at = started
            try:
                if self.config.requested_mode == "LIVE" and not self.config.live_ready:
                    missing = ", ".join(self.config.missing_live_requirements)
                    raise RuntimeError(f"explicit LIVE mode is not ready; missing: {missing}")
                if self._should_attempt_live():
                    events, cycle, source = await asyncio.to_thread(self._live_cycle)
                    source_mode: SourceMode = "LIVE"
                else:
                    events, cycle, source = self._replay_cycle()
                    source_mode = "REPLAY"
                await self._record_success(events, cycle, source, source_mode)
                return cycle
            except Exception as exc:  # runtime must remain inspectable after provider failure
                safe_error = self._safe_error(exc)
                await self._record_error(safe_error)
                if self.config.replay_fallback:
                    events, cycle, source = self._replay_cycle()
                    source = {**source, "fallback_reason": safe_error}
                    await self._record_success(events, cycle, source, "REPLAY_FALLBACK", preserve_error=True)
                    return cycle
                async with self._state_lock:
                    self._source_mode = "ERROR"
                    self._last_cycle_finished_at = self._clock()
                raise

    def _should_attempt_live(self) -> bool:
        if self.config.requested_mode == "REPLAY":
            return False
        return self.config.live_ready

    def _live_cycle(self) -> tuple[list[MarketEvent], dict[str, Any], dict[str, Any]]:
        if not self.config.fixture_id:
            raise ValueError("PROOFGUARD_FIXTURE_ID is required for live mode")
        client = self._client_factory(self.config.txline)
        odds_payload = client.odds_snapshot(self.config.fixture_id)
        observed_at = self._clock()
        events = events_from_odds_payload(
            odds_payload,
            fixture_id=self.config.fixture_id,
            model_probabilities=self.config.model_probabilities,
            observed_at=observed_at,
        )
        score_summary: dict[str, Any] = {
            "requested": self.config.with_scores,
            "available": False,
            "status": "UNAVAILABLE" if self.config.with_scores else "DISABLED",
            "fixture_final": False,
            "winning_selection": None,
        }
        if self.config.with_scores:
            try:
                score_payload = client.scores_snapshot(self.config.fixture_id)
                events, normalized = apply_score_finality(
                    events,
                    score_payload,
                    fixture_id=self.config.fixture_id,
                )
                score_summary = {"requested": True, "available": True, "status": "AVAILABLE", **normalized}
            except Exception as exc:
                score_summary = {
                    **score_summary,
                    "error": self._safe_error(exc),
                }
        # In-play model: when a live score is available, recompute each model
        # probability from the deterministic prior+score model so the agent's
        # edge reacts to goals rather than using a static pre-match estimate.
        model_summary: dict[str, Any] = {"applied": False, "reason": "no_live_score"}
        home = score_summary.get("home_score")
        away = score_summary.get("away_score")
        if isinstance(home, int) and isinstance(away, int):
            model_probs = match_result_probabilities(self.config.model_probabilities, home_score=home, away_score=away)
            events = [replace(event, model_probability=model_probs.get(event.selection, event.model_probability)) for event in events]
            model_summary = {
                "applied": True,
                "home_score": home,
                "away_score": away,
                "probabilities": {sel: round(value, 6) for sel, value in model_probs.items()},
            }

        cycle = self._agent.process(events)
        source = {
            "mode": "LIVE",
            "provider": "TxLINE",
            "origin": self.config.txline.origin,
            "fixture_id": self.config.fixture_id,
            "odds_schema_fingerprint": events[0].source_fingerprint if events else None,
            "score_summary": score_summary,
            "in_play_model": model_summary,
            "raw_payload_persisted": False,
            "credentials_exposed": False,
        }
        return events, cycle, source

    def _replay_cycle(self) -> tuple[list[MarketEvent], dict[str, Any], dict[str, Any]]:
        now = self._clock()
        step_index = self._replay_index % len(REPLAY_SCENARIO)
        step = REPLAY_SCENARIO[step_index]
        self._replay_index += 1
        event_kwargs = {key: value for key, value in step.items() if key not in {"tag", "minute", "narrative"}}
        event = self._replay_event(
            now,
            event_id=f"replay-{self._replay_index:04d}-{step['tag']}",
            **event_kwargs,
        )
        events = [event]
        cycle = self._agent.process(events)
        source = {
            "mode": "REPLAY",
            "provider": "bundled deterministic replay",
            "fixture_id": event.fixture_id,
            "replay_phase": step_index,  # retained for back-compat
            "scenario_step": step_index + 1,
            "scenario_total": len(REPLAY_SCENARIO),
            "match_minute": step["minute"],
            "narrative": step["narrative"],
            "raw_payload_persisted": False,
            "credentials_exposed": False,
        }
        return events, cycle, source

    @staticmethod
    def _replay_event(
        observed_at: datetime,
        *,
        event_id: str,
        selection: str,
        market_probability: float,
        model_probability: float,
        probability_sum: float = 1.0,
        stale_seconds: float = 10.0,
        proof_ready: bool = True,
        backwards: bool = False,
        fixture_final: bool = False,
        winning_selection: str | None = None,
    ) -> MarketEvent:
        fair_probability = min(max(market_probability / probability_sum, 1e-9), 1.0 - 1e-9) if probability_sum > 0 else None
        return MarketEvent(
            event_id=event_id,
            fixture_id="wc-proofguard-replay-001",
            market="MATCH_RESULT",
            selection=selection,
            market_probability=market_probability,
            model_probability=model_probability,
            market_probability_sum=probability_sum,
            fair_probability=fair_probability,
            stale_seconds=stale_seconds,
            proof_ready=proof_ready,
            backwards_timestamp=backwards,
            observed_at=observed_at,
            fixture_final=fixture_final,
            winning_selection=winning_selection,
            source_fingerprint="proofguard-replay-schema-v1",
        )

    async def _record_success(
        self,
        events: list[MarketEvent],
        cycle: dict[str, Any],
        source: dict[str, Any],
        source_mode: SourceMode,
        *,
        preserve_error: bool = False,
    ) -> None:
        finished = self._clock()
        records = cycle.get("records", [])
        async with self._state_lock:
            self._cycle_count += 1
            self._source_mode = source_mode
            self._latest_events = [event.to_dict() for event in events]
            self._latest_cycle = cycle
            self._latest_source = source
            for record in records:
                if isinstance(record, dict):
                    self._receipts.append(record)
            self._last_cycle_finished_at = finished
            self._last_success_at = finished
            if not preserve_error:
                self._consecutive_errors = 0
                self._last_error = None

    async def _record_error(self, error: str) -> None:
        now = self._clock()
        async with self._state_lock:
            self._consecutive_errors += 1
            self._last_error = error
            self._last_cycle_finished_at = now
            self._recent_errors.append({"at": _iso(now), "error": error})

    def _safe_error(self, exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}"
        for secret in (self.config.txline.guest_jwt, self.config.txline.api_token):
            if secret:
                text = text.replace(secret, "<redacted>")
        return text[:500]

    def _decision_distribution(self) -> dict[str, Any]:
        """Honest analytics over the actual receipt history (no synthetic data).

        Counts every decision the agent has emitted this session by action and by
        the integrity-gate verdict that governed it. Must be called under the
        state lock. The invariant the dashboard advertises — no ENTER on a
        non-PASS gate — is computed here from the receipts themselves.
        """
        actions: dict[str, int] = {}
        gates: dict[str, int] = {}
        unsafe_enters = 0
        for record in self._receipts:
            if not isinstance(record, dict):
                continue
            action = str(record.get("action", "UNKNOWN"))
            gate = str(record.get("integrity", {}).get("decision", "UNKNOWN"))
            actions[action] = actions.get(action, 0) + 1
            gates[gate] = gates.get(gate, 0) + 1
            if action == "ENTER" and gate != "PASS":
                unsafe_enters += 1
        total = len(self._receipts)
        return {
            "total_decisions": total,
            "by_action": actions,
            "by_integrity_gate": gates,
            "unsafe_enter_count": unsafe_enters,
            "safety_invariant_holds": unsafe_enters == 0,
        }

    def _integrity_impact(self) -> dict[str, Any]:
        """Quantify ProofGuard's value against a *naive edge-only baseline*.

        The naive baseline is an agent identical to ProofGuard except that it
        ignores the integrity gate: it enters any signal clearing the same edge
        and confidence thresholds. Every such signal whose integrity verdict was
        REVIEW or BLOCK is an "integrity exploit" — a trade the naive agent would
        have taken and ProofGuard refused. This is a deterministic counterfactual
        over the real receipts (no synthetic P&L is asserted): we report how many
        exploits were blocked and the paper exposure a naive agent would have put
        at risk, capped at the per-signal maximum. Must be called under the lock.
        """
        naive_entries = 0
        exploits_blocked = 0
        exposure_at_risk_prevented = 0.0
        attack_edges: list[float] = []
        for record in self._receipts:
            if not isinstance(record, dict):
                continue
            signal = record.get("signal", {})
            controls = record.get("controls", {})
            edge = signal.get("edge")
            confidence = signal.get("confidence")
            min_edge = controls.get("minimum_edge")
            confidence_floor = controls.get("confidence_floor")
            max_stake = controls.get("maximum_stake_fraction", 0.0)
            if not all(isinstance(v, (int, float)) for v in (edge, confidence, min_edge, confidence_floor)):
                continue
            naive_would_enter = edge >= min_edge and confidence >= confidence_floor
            if not naive_would_enter:
                continue
            naive_entries += 1
            if str(record.get("integrity", {}).get("decision")) != "PASS":
                exploits_blocked += 1
                exposure_at_risk_prevented += float(max_stake)
                attack_edges.append(float(edge))
        largest = max(attack_edges) if attack_edges else 0.0
        return {
            "baseline": "naive edge-only agent (enters any signal clearing the same edge + confidence thresholds, ignoring the integrity gate)",
            "naive_entry_signals": naive_entries,
            "integrity_exploits_blocked": exploits_blocked,
            "paper_exposure_at_risk_prevented": round(exposure_at_risk_prevented, 4),
            "largest_blocked_edge": round(largest, 4),
            "note": "deterministic counterfactual over the real receipts; no monetary P&L is claimed",
        }

    async def snapshot(self) -> dict[str, Any]:
        async with self._state_lock:
            # Deep-copy internal mutable state so a caller that mutates the
            # returned snapshot can never corrupt the runtime. Each datum is
            # copied exactly once; latest_decision is taken from the copied
            # cycle rather than copied again.
            latest_cycle = copy.deepcopy(self._latest_cycle)
            latest_record = None
            if latest_cycle and latest_cycle.get("records"):
                latest_record = latest_cycle["records"][-1]
            distribution = self._decision_distribution()
            integrity_impact = self._integrity_impact()
            return {
                "schema": "proofguard.live-dashboard.v1",
                "service": "ProofGuard Autonomous Agent",
                "status": "DEGRADED" if self._last_error else "RUNNING",
                "source_mode": self._source_mode,
                "task_running": self.task_running,
                "started_at": _iso(self._started_at),
                "last_cycle_started_at": _iso(self._last_cycle_started_at),
                "last_cycle_finished_at": _iso(self._last_cycle_finished_at),
                "last_success_at": _iso(self._last_success_at),
                "cycle_count": self._cycle_count,
                "consecutive_errors": self._consecutive_errors,
                "last_error": self._last_error,
                "configuration": self.config.public_view(),
                "source": copy.deepcopy(self._latest_source),
                "market": copy.deepcopy(self._latest_events),
                "latest_decision": latest_record,
                "latest_cycle": latest_cycle,
                "portfolio": self._agent.portfolio_snapshot(),
                "decision_distribution": distribution,
                "integrity_impact": integrity_impact,
                "receipts": copy.deepcopy(list(reversed(self._receipts))),
                "recent_errors": copy.deepcopy(list(reversed(self._recent_errors))),
                "safety": {
                    "simulation_only": True,
                    "raw_payload_persisted": False,
                    "credentials_exposed": False,
                    "wallet_connected": False,
                    "real_money_execution": False,
                },
                "claim_boundary": "live or replay-driven autonomous paper decisions only; no wagering, custody, crypto-asset execution, investment advice, or profitability guarantee",
            }

    async def health(self) -> dict[str, Any]:
        # Read only the few fields the health contract needs, directly under the
        # lock. This avoids building/copying the full snapshot (receipts, market,
        # cycle, portfolio) on every health probe. The lock is held only for
        # cheap scalar reads; readiness is derived from the immutable config
        # outside the lock. Response schema and values are identical to before.
        async with self._state_lock:
            source_mode = self._source_mode
            task_running = self.task_running
            last_success_at = _iso(self._last_success_at)
            cycle_count = self._cycle_count
        live_ready = self.config.live_ready
        explicit_live_misconfigured = self.config.requested_mode == "LIVE" and live_ready is False
        return {
            "status": "FAIL" if source_mode == "ERROR" or explicit_live_misconfigured else "PASS",
            "source_mode": source_mode,
            "task_running": task_running,
            "last_success_at": last_success_at,
            "cycle_count": cycle_count,
            "live_ready": live_ready,
            "simulation_only": True,
        }
