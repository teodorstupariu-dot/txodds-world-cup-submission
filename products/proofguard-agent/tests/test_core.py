from __future__ import annotations

from datetime import datetime, timezone

from proofguard_agent import (
    GENESIS_RECEIPT,
    MarketEvent,
    ProofGuardAutonomousAgent,
    verify_receipt,
    verify_receipt_chain,
)

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def event(
    event_id: str = "event-1",
    *,
    selection: str = "HOME",
    market_probability: float = 0.45,
    model_probability: float = 0.65,
    probability_sum: float = 1.0,
    stale_seconds: float = 10.0,
    proof_ready: bool = True,
    backwards: bool = False,
    fixture_final: bool = False,
    winning_selection: str | None = None,
) -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        fixture_id="fixture-1",
        market="MATCH_RESULT",
        selection=selection,
        market_probability=market_probability,
        model_probability=model_probability,
        market_probability_sum=probability_sum,
        stale_seconds=stale_seconds,
        proof_ready=proof_ready,
        backwards_timestamp=backwards,
        observed_at=NOW,
        fixture_final=fixture_final,
        winning_selection=winning_selection,
    )


def first_record(cycle):
    return cycle["records"][0]


def test_clean_edge_opens_paper_position() -> None:
    agent = ProofGuardAutonomousAgent()
    cycle = agent.process([event()])
    record = first_record(cycle)

    assert record["integrity"]["decision"] == "PASS"
    assert record["action"] == "ENTER"
    assert record["execution"] == "OPEN"
    assert cycle["portfolio"]["open_position_count"] == 1
    assert cycle["portfolio"]["total_exposure"] == 0.02
    assert verify_receipt(record)["status"] == "PASS"


def test_review_integrity_forces_hold() -> None:
    agent = ProofGuardAutonomousAgent()
    cycle = agent.process([event(proof_ready=False)])
    record = first_record(cycle)

    assert record["integrity"]["decision"] == "REVIEW"
    assert record["action"] == "HOLD"
    assert cycle["portfolio"]["open_position_count"] == 0


def test_block_integrity_forces_reject_despite_large_edge() -> None:
    agent = ProofGuardAutonomousAgent()
    cycle = agent.process([event(backwards=True, stale_seconds=300, probability_sum=1.2)])
    record = first_record(cycle)

    assert record["integrity"]["decision"] == "BLOCK"
    assert record["action"] == "REJECT"
    assert cycle["safety"]["unsafe_entry_count"] == 0


def test_low_edge_holds_on_clean_market() -> None:
    agent = ProofGuardAutonomousAgent()
    cycle = agent.process([event(market_probability=0.45, model_probability=0.46)])
    assert first_record(cycle)["action"] == "HOLD"


def test_reduced_risk_resizes_position() -> None:
    agent = ProofGuardAutonomousAgent()
    first = agent.process([event()])
    agent.set_risk_mode("reduced")
    second = agent.process([event(event_id="event-2")])

    assert first["portfolio"]["total_exposure"] == 0.02
    assert first_record(second)["execution"] == "RESIZE"
    assert second["portfolio"]["total_exposure"] == 0.007


def test_kill_switch_closes_and_rejects() -> None:
    agent = ProofGuardAutonomousAgent()
    agent.process([event()])
    agent.set_kill_switch(True)
    cycle = agent.process([event(event_id="kill")])

    assert cycle["kill_switch_closures"]
    assert first_record(cycle)["action"] == "REJECT"
    assert cycle["portfolio"]["total_exposure"] == 0.0


def test_fixture_final_closes_paper_position() -> None:
    agent = ProofGuardAutonomousAgent()
    agent.process([event()])
    cycle = agent.process([event(event_id="final", fixture_final=True, winning_selection="HOME", market_probability=0.95, model_probability=0.95)])

    assert first_record(cycle)["action"] == "CLOSE"
    assert cycle["portfolio"]["open_position_count"] == 0
    assert cycle["portfolio"]["total_exposure"] == 0.0


def test_total_exposure_is_capped() -> None:
    agent = ProofGuardAutonomousAgent(maximum_stake_fraction=0.06, maximum_total_exposure=0.08)
    cycle = agent.process([
        event(event_id="home", selection="HOME"),
        event(event_id="away", selection="AWAY"),
    ])

    assert cycle["portfolio"]["total_exposure"] <= 0.08
    assert cycle["safety"]["exposure_within_limit"] is True


def test_receipt_tampering_is_detected() -> None:
    agent = ProofGuardAutonomousAgent()
    record = first_record(agent.process([event()]))
    assert verify_receipt(record)["status"] == "PASS"
    record["action"] = "REJECT"
    assert verify_receipt(record)["status"] == "FAIL"


def test_identical_fresh_agents_produce_identical_receipts() -> None:
    first = first_record(ProofGuardAutonomousAgent().process([event()]))["receipt_sha256"]
    second = first_record(ProofGuardAutonomousAgent().process([event()]))["receipt_sha256"]
    assert first == second


def test_fractional_kelly_sizes_below_cap_when_edge_is_modest() -> None:
    # With a generous stake cap, the stake reflects half-Kelly, not the cap.
    agent = ProofGuardAutonomousAgent(maximum_stake_fraction=0.10, maximum_total_exposure=0.10)
    record = first_record(agent.process([event(market_probability=0.50, model_probability=0.55)]))
    assert record["action"] == "ENTER"
    # Kelly f* = (b*p-(1-p))/b with b=1, p=0.55 -> 0.10; half-Kelly -> 0.05.
    assert abs(record["target_stake_fraction"] - 0.05) < 1e-6


def test_auto_reduced_risk_engages_on_flag_streak_and_releases_when_clean() -> None:
    agent = ProofGuardAutonomousAgent(integrity_flag_threshold=2)
    agent.process([event(proof_ready=False)])            # REVIEW, flags=1
    assert agent.risk_mode == "normal"
    cycle = agent.process([event(proof_ready=False, event_id="r2")])  # REVIEW, flags=2 -> reduced
    assert agent.risk_mode == "reduced"
    assert "auto_reduced_risk_engaged" in cycle["auto_control_actions"]
    assert cycle["portfolio"]["auto_controls"]["auto_reduced_engaged"] is True
    released = agent.process([event(event_id="clean")])   # clean -> released
    assert agent.risk_mode == "normal"
    assert "auto_reduced_risk_released" in released["auto_control_actions"]


def test_auto_kill_switch_engages_on_integrity_storm() -> None:
    agent = ProofGuardAutonomousAgent(integrity_block_storm_threshold=3)
    block = dict(backwards=True, stale_seconds=300.0, probability_sum=1.2)
    for i in range(2):
        agent.process([event(event_id=f"b{i}", **block)])
        assert agent.kill_switch is False
    storm = agent.process([event(event_id="b2", **block)])       # third consecutive BLOCK
    assert agent.kill_switch is True
    assert "auto_kill_switch_engaged_integrity_storm" in storm["auto_control_actions"]
    # Kill switch is sticky: the next clean signal is rejected, not entered.
    after = first_record(agent.process([event(event_id="clean")]))
    assert after["action"] == "REJECT"


def _all_records(agent: ProofGuardAutonomousAgent, cycles: list[list[MarketEvent]]) -> list[dict]:
    records: list[dict] = []
    for events in cycles:
        records.extend(agent.process(events)["records"])
    return records


def test_receipt_chain_links_across_cycles() -> None:
    agent = ProofGuardAutonomousAgent()
    records = _all_records(
        agent,
        [
            [event(event_id="c1")],
            [event(event_id="c2", selection="AWAY", market_probability=0.3, model_probability=0.31)],
            [event(event_id="c3", fixture_final=True, winning_selection="HOME", market_probability=0.95, model_probability=0.95)],
        ],
    )

    # Genesis anchor + contiguous sequence + each links to its parent.
    assert records[0]["prev_receipt_sha256"] == GENESIS_RECEIPT
    assert [r["sequence"] for r in records] == [0, 1, 2]
    for parent, child in zip(records, records[1:]):
        assert child["prev_receipt_sha256"] == parent["receipt_sha256"]

    verdict = verify_receipt_chain(records)
    assert verdict["status"] == "PASS"
    assert verdict["checked"] == 3
    assert verdict["head_sha256"] == records[-1]["receipt_sha256"]


def test_receipt_chain_detects_reorder() -> None:
    agent = ProofGuardAutonomousAgent()
    records = _all_records(agent, [[event(event_id="a")], [event(event_id="b")]])
    assert verify_receipt_chain(records)["status"] == "PASS"
    swapped = [records[1], records[0]]
    assert verify_receipt_chain(swapped)["status"] == "FAIL"


def test_receipt_chain_detects_edit_of_past_decision() -> None:
    agent = ProofGuardAutonomousAgent()
    records = _all_records(agent, [[event(event_id="a")], [event(event_id="b")]])
    assert verify_receipt_chain(records)["status"] == "PASS"
    # Tamper with a past decision's content: chain must fail (integrity break).
    records[0]["action"] = "REJECT"
    assert verify_receipt_chain(records)["status"] == "FAIL"
