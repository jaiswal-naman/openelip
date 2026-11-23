"""Plan 07-03, GRD-03: the flight recorder's writer and readers."""

import threading

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.platform import db
from tests.guardian.support import add_spend, entails, make_committed_world, propose_follow_up, rows, scalar


def test_seq_increases_per_run_under_concurrent_writers(engine, clean):
    world = make_committed_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "researcher", "test-1")
    errors: list[Exception] = []

    def writer(label: str) -> None:
        try:
            for n in range(5):
                with db.transaction(world.user, role="worker") as conn:
                    guardian.record(conn, run, kind="tool_call", summary=f"{label} {n}")
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [threading.Thread(target=writer, args=(name,)) for name in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    seqs = [r[0] for r in rows(engine, "SELECT seq FROM agent_actions WHERE agent_run_id = :r ORDER BY seq", r=run)]
    assert seqs == list(range(1, 16))


def test_record_refuses_the_kinds_other_writers_own(engine, clean):
    world = make_committed_world(engine)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "researcher", "test-1")
        for kind in ("outside_action", "state_change"):
            with pytest.raises(ValueError):
                guardian.record(conn, run, kind=kind, summary="x")


def test_the_timeline_lists_entries_in_order_with_checks_cost_and_approval(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    call = add_spend(engine, world, 12_000)
    other = add_spend(engine, world, 30_000)
    with db.transaction(world.user, role="worker") as conn:
        run = guardian.open_run(conn, world.user, world.campaign, "chaser", "test-1")
        guardian.record(
            conn,
            run,
            kind="model_call",
            summary="Drafted.",
            inputs={"llm_call_ids": [call, other]},
            evidence=[{"fact_id": "x"}],
        )
        proposed = guardian.propose_action(
            conn,
            run,
            action_type="send_follow_up",
            payload={
                "channel": "email",
                "to": "talent@lumen.example",
                "reply_to": "a@example.test",
                "subject": "s",
                "body": "Hello,\n\nI applied on 3 November 2026.\n\nBest regards,\nAda",
            },
            provenance={
                "fields": {"subject": "code"},
                "sentences": [
                    {
                        "field": "body",
                        "text": "I applied on 3 November 2026.",
                        "events": ["I applied on 3 November 2026."],
                    }
                ],
                "code_parts": ["Hello,", "Best regards,\nAda"],
            },
        )
    with db.transaction(world.user) as conn:
        entries = guardian.timeline(conn, world.user, campaign_id=world.campaign)
        by_run = guardian.timeline(conn, world.user, run_id=run)
    assert [e["seq"] for e in entries] == [1, 2] and entries == by_run
    model, outside = entries
    assert (model["kind"], model["status"], model["cost_microusd"], model["evidence_count"]) == (
        "model_call",
        "recorded",
        42_000,
        1,
    )
    assert list(outside) == [
        "id",
        "run_id",
        "seq",
        "created_at",
        "agent_role",
        "kind",
        "action_type",
        "status",
        "summary",
        "guardian",
        "approval",
        "decided_at",
        "executed_at",
        "outcome_code",
        "cost_microusd",
        "evidence_count",
    ]
    assert [g["check"] for g in outside["guardian"]] == [
        "citations",
        "consent",
        "scope",
        "budget",
        "injection",
        "rate_limit",
    ]
    assert (outside["approval"], outside["status"], outside["agent_role"]) == ("pending", "awaiting_approval", "chaser")
    assert outside["id"] == proposed.action_id


def test_an_entry_in_full_and_another_persons_entry_is_invisible(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    p = propose_follow_up(world)
    with db.transaction(world.user) as conn:
        found = guardian.entry(conn, world.user, p.action_id)
    assert found is not None
    assert found["policy_decision"] == "ask" and found["policy_version"] == guardian.POLICY_VERSION
    assert found["payload"]["to"] == "talent@lumen.example" and found["inputs"]["step_id"] == "follow_up:1"
    assert len(found["checks"]) == 6 and {c["guardian_version"] for c in found["checks"]} == {guardian.GUARDIAN_VERSION}
    assert found["approval"]["status"] == "pending" and found["agent_role"] == "chaser"
    other = make_committed_world(engine, consent=False)
    with db.transaction(other.user) as conn:
        assert guardian.entry(conn, other.user, p.action_id) is None
        assert guardian.timeline(conn, other.user, campaign_id=world.campaign) == []


def test_last_state_change_finds_the_newest_row_with_the_code(engine, clean):
    world = make_committed_world(engine)
    with db.transaction(world.user) as conn:
        conn.execute(text("SELECT record_state_change(:c, 'one', 'cancelled', '{}')"), {"c": world.campaign})
        conn.execute(text("SELECT record_state_change(:c, 'two', 'cancelled', '{\"n\": 2}')"), {"c": world.campaign})
        newest = guardian.last_state_change(conn, world.campaign, "cancelled")
        assert newest is not None and newest["summary"] == "two" and newest["inputs"] == {"n": 2}
        assert guardian.last_state_change(conn, world.campaign, "nothing") is None
    assert scalar(engine, "SELECT count(*) FROM agent_runs WHERE agent_role = 'guardian'") == 1
