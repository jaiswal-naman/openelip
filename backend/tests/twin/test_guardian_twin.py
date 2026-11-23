"""The Guardian takes `twin_answer` (plan 09-03, Task 1): the policy table, the six checks' Twin inputs, the subject
reader, the events that wake the Twin's thread and the approve hook. No model call is made here: proposals are built by
hand on a fixture session. Everything is invented."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text

from app.guardian import api as guardian
from app.guardian import approvals, budget, checks, explain, policy
from app.platform import db
from app.platform.errors import ApiError
from app.twin import api as twin
from app.twin import logic
from app.twin import subject as twin_subject
from tests.guardian.conftest import clean  # noqa: F401  (empties the tables around a committing test)
from tests.twin.fixtures.graph import world as w

GATE = guardian.SubjectGate("twin", UUID(int=1), UUID(int=2), True, None, False, 0, UUID(int=3), None, 0)
ANSWER = policy.TwinPolicy("answer", "answer")


def verdict(kind="answer", permission="answer", dial="tell_me_after", gate=GATE):
    result = policy.decide("twin_answer", dial=dial, gate=gate, twin=policy.TwinPolicy(kind, permission))
    return result.decision, result.reason_code


# ---- key one ---------------------------------------------------------------------------------------------------------


def test_the_standing_yes_is_the_only_allow():
    assert verdict() == ("allow", None)
    for kind, permission, dial in [
        ("answer", "answer", "ask_first"),
        ("answer", "ask_me", "tell_me_after"),
        ("relay", "answer", "tell_me_after"),
        ("relay", "ask_me", "ask_first"),
    ]:
        assert verdict(kind, permission, dial)[0] == "ask", (kind, permission, dial)


def test_a_closed_topic_is_denied_for_answers_and_relays_alike():
    assert verdict("answer", "never") == ("deny", "topic_not_open")
    assert verdict("relay", "never", "ask_first") == ("deny", "topic_not_open")


def test_dial_never_denies_a_draft_but_not_the_candidates_own_reply():
    assert verdict("answer", "answer", "never") == ("deny", "dial_never")
    assert verdict("relay", "answer", "never") == ("ask", None)


def test_a_closed_session_and_an_inactive_account_are_denied_first():
    assert verdict(gate=replace(GATE, closed=True)) == ("deny", "session_closed")
    assert verdict(gate=replace(GATE, user_active=False)) == ("deny", "account_inactive")
    assert verdict(gate=None) == ("deny", "type_not_available")


def test_a_twin_answer_for_a_campaign_subject_or_without_twin_facts_is_not_available():
    campaign = replace(GATE, kind="campaign")
    result = policy.decide("twin_answer", dial="tell_me_after", gate=campaign, twin=ANSWER)
    assert (result.decision, result.reason_code) == ("deny", "type_not_available")
    assert policy.decide("twin_answer", dial="ask_first", gate=GATE).reason_code == "type_not_available"


def test_the_versions_and_shared_constants_are_named():
    assert policy.POLICY_VERSION == "policy-2026-11-b" and checks.GUARDIAN_VERSION == "guardian-2026-11-b"
    assert checks.SESSION_QUESTIONS == twin.SESSION_QUESTIONS == 40
    assert checks.SESSION_COST_MICROUSD == 500_000
    assert (
        checks.TWIN_FACT_TOPIC == logic.FACT_TOPIC
    )  # the Guardian may not import the Twin, so a test keeps them equal


@pytest.mark.parametrize("code", ["topic_not_open", "session_closed", "restricted_data", "limit_reached"])
def test_the_new_codes_are_explained(code):
    assert explain.explain(code, detail="your pay")


# ---- proposals on a fixture session ----------------------------------------------------------------------------------


def fact(world: w.GWorld, number: int) -> str:
    """The id of `f1`.. (the three resume facts, in order) as a string."""
    return str(world.world.candidate.record.fact_ids[number - 1])


def propose(
    engine: Engine,
    world: w.GWorld,
    *,
    kind: str = "answer",
    topic: str = "experience",
    sentences: list[tuple[str, list[str]]] | None = None,
    body: str | None = None,
    question: UUID | None = None,
    dial_inputs: bool = True,
) -> guardian.Proposed:
    question = question or _question(engine, world)
    sentences = sentences if sentences is not None else [("Led a team of three engineers.", [fact(world, 3)])]
    payload: dict[str, Any] = {
        "kind": kind,
        "question_id": str(question),
        "session_id": str(world.session),
        "introduction_id": str(world.world.introduction),
        "organization_id": str(world.world.org.id),
        "topic": topic,
        "body": body if body is not None else (" ".join(t for t, _ in sentences) if kind == "answer" else None),
        "sentences": [{"text": t, "fact_ids": ids} for t, ids in sentences] if kind == "answer" else [],
        "cited_fact_ids": sorted({i for _, ids in sentences for i in ids}) if kind == "answer" else [],
    }
    provenance = {
        "fields": {"body": "model" if kind == "answer" else "candidate"},
        "sentences": [{"field": "body", "text": t, "fact_ids": ids} for t, ids in sentences]
        if kind == "answer"
        else [],
    }
    with db.transaction(world.candidate, role="worker") as conn:
        run = conn.execute(
            text(
                """INSERT INTO agent_runs (user_id, agent_role, config_version, twin_session_id)
                   VALUES (:u, 'twin', 'test', :s) RETURNING id"""
            ),
            {"u": world.candidate, "s": world.session},
        ).scalar_one()
        return guardian.propose_action(
            conn,
            run,
            action_type="twin_answer",
            payload=payload,
            provenance=provenance,
            organization_id=world.world.org.id,
            inputs={"question_id": str(question), "kind": kind},
        )


def _question(engine: Engine, world: w.GWorld, body: str = "What did you lead?") -> UUID:
    with engine.begin() as c:
        return c.execute(  # type: ignore[no-any-return]
            text(
                """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
                   VALUES (:s, :u, :o, 'employer', :b) RETURNING id"""
            ),
            {"s": world.session, "u": world.candidate, "o": world.world.org.id, "b": body},
        ).scalar_one()


def failed(engine: Engine, action_id: int) -> list[tuple[str, str]]:
    return [
        (r.check_kind, r.reason_code)
        for r in w.rows(
            engine,
            """SELECT check_kind::text AS check_kind, reason_code FROM guardian_checks
                WHERE agent_action_id = :a AND NOT passed ORDER BY id""",
            a=action_id,
        )
    ]


def resumes(engine: Engine) -> list[str]:
    return [
        r.cause
        for r in w.rows(
            engine, "SELECT payload ->> 'cause' AS cause FROM queue_jobs WHERE kind = 'twin.resume' ORDER BY id"
        )
    ]


def test_the_standing_yes_ends_scheduled_with_six_checks_and_queues_no_resume(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    proposed = propose(engine, world)
    assert (proposed.status, proposed.approval_id, proposed.failed_codes) == ("scheduled", None, ())
    assert w.one(engine, "SELECT count(*) FROM guardian_checks WHERE agent_action_id = :a", a=proposed.action_id) == 6
    assert (
        w.one(engine, "SELECT policy_decision::text FROM agent_actions WHERE id = :a", a=proposed.action_id) == "allow"
    )
    assert resumes(engine) == []  # propose_action never publishes: the graph handles its result in the same run


def test_other_answers_wait_for_one_approval_and_queue_no_resume(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    proposed = propose(engine, world)
    assert proposed.status == "awaiting_approval" and proposed.approval_id is not None
    assert resumes(engine) == []


def test_a_draft_citing_a_fact_in_a_closed_topic_fails_the_citation_check(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "never"})
    proposed = propose(engine, world, topic="skills")
    assert proposed.status == "blocked" and "citation_unresolved" in proposed.failed_codes
    assert ("citations", "citation_unresolved") in failed(engine, proposed.action_id)
    assert resumes(engine) == []  # a blocked proposal never wakes the thread either


def test_a_sentence_citing_a_fact_that_is_not_the_candidates_or_not_confirmed_fails_closed(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    for ids in ([str(UUID(int=99))], []):
        proposed = propose(engine, world, sentences=[("Led a team of three engineers.", ids)])
        assert proposed.status == "blocked" and proposed.failed_codes[0] == "citation_unresolved"


def test_a_relay_skips_citations_and_injection(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    proposed = propose(engine, world, kind="relay", sentences=[])
    assert proposed.status == "awaiting_approval"
    skipped = w.rows(
        engine,
        """SELECT check_kind::text AS k, details FROM guardian_checks WHERE agent_action_id = :a
            AND check_kind IN ('citations', 'injection') ORDER BY id""",
        a=proposed.action_id,
    )
    assert [(r.k, r.details) for r in skipped] == [("citations", {"applies": False}), ("injection", {"applies": False})]


@pytest.mark.parametrize(
    "text_",
    ["You can call me on +91 98765 43210.", "Mail ada@example.test.", "My salary is fine.", "Our CTC is open."],
)
def test_scope_blocks_pay_and_contact_details_in_a_body_a_sentence_and_a_relay(engine, clean, text_):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    drafted = propose(engine, world, sentences=[(text_, [fact(world, 3)])])
    assert drafted.status == "blocked" and ("scope", "restricted_data") in failed(engine, drafted.action_id)
    relay = propose(engine, world, kind="relay", sentences=[], body=text_)
    assert relay.status == "blocked" and ("scope", "restricted_data") in failed(engine, relay.action_id)


def test_scope_blocks_a_cited_fact_whose_statement_holds_contact_details(engine, clean):  # noqa: F811
    world = w.build(
        engine,
        dial="tell_me_after",
        topics={"experience": "answer", "other": "answer"},
        facts=(("phone", "other", "Reach me on +91 98765 43210 about the migration."),),
    )
    proposed = propose(
        engine, world, topic="other", sentences=[("I can talk about the migration.", [str(world.extra["phone"])])]
    )
    assert proposed.status == "blocked" and ("scope", "restricted_data") in failed(engine, proposed.action_id)


def test_scope_blocks_a_closed_topic_for_drafts_and_relays_alike(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer", "projects": "never"})
    for kind in ("answer", "relay"):
        proposed = propose(engine, world, kind=kind, topic="projects")
        assert proposed.status == "blocked"
        codes = [
            row.reason_code
            for row in w.rows(
                engine, "SELECT outcome_code AS reason_code FROM agent_actions WHERE id = :a", a=proposed.action_id
            )
        ]
        assert codes == ["topic_not_open"]  # key one denies a closed topic before the checks can


def test_a_payload_naming_another_session_is_refused_by_scope(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    other = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    question = _question(engine, world)
    with db.transaction(world.candidate, role="worker") as conn:
        run = conn.execute(
            text(
                """INSERT INTO agent_runs (user_id, agent_role, config_version, twin_session_id)
                   VALUES (:u, 'twin', 't', :s) RETURNING id"""
            ),
            {"u": world.candidate, "s": world.session},
        ).scalar_one()
        proposed = guardian.propose_action(
            conn,
            run,
            action_type="twin_answer",
            payload={
                "kind": "relay",
                "question_id": str(question),
                "session_id": str(other.session),
                "introduction_id": str(world.world.introduction),
                "organization_id": str(world.world.org.id),
                "topic": "experience",
                "body": "Yes.",
                "sentences": [],
                "cited_fact_ids": [],
            },
            provenance={"fields": {"body": "candidate"}},
            inputs={"question_id": str(question), "kind": "relay"},
        )
    assert proposed.status == "blocked" and ("scope", "restricted_data") in failed(engine, proposed.action_id)


def test_consent_fails_after_the_introduction_consent_is_withdrawn(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    from tests.twin.factories import withdraw

    question = _question(engine, world)  # the database takes no new question once the consent is withdrawn
    with engine.begin() as c:
        withdraw(c, world.world.accepted.consent)
    proposed = propose(engine, world, question=question)
    assert proposed.status == "blocked" and ("consent", "consent_missing") in failed(engine, proposed.action_id)


def test_injection_blocks_an_address_the_cited_fact_does_not_hold(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    proposed = propose(engine, world, sentences=[("Read more at www.example.test/me.", [fact(world, 3)])])
    assert proposed.status == "blocked" and ("injection", "injection_suspected") in failed(engine, proposed.action_id)


def test_the_session_caps_hold_at_the_41st_question_and_at_fifty_cents(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    w.add_spend(engine, world, 499_999)
    assert propose(engine, world).status == "awaiting_approval"
    w.add_spend(engine, world, 1)
    proposed = propose(engine, world)
    assert proposed.status == "blocked" and ("rate_limit", "limit_reached") in failed(engine, proposed.action_id)
    other = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    for _ in range(39):
        _question(engine, other)
    assert (
        propose(engine, other).status == "awaiting_approval"
    )  # this one is the 40th question: the last the intake takes
    _question(engine, other)  # a 41st (the intake refuses it; the Guardian does too)
    assert ("rate_limit", "limit_reached") in failed(engine, propose(engine, other).action_id)


def test_the_twin_subject_reader_gives_the_session_and_a_closed_session_reads_closed(engine, clean):  # noqa: F811
    world = w.build(engine)
    with db.transaction(world.candidate, role="worker") as conn:
        gate = twin_subject.read_gate(conn, world.session)
        assert (gate.kind, gate.id, gate.user_id, gate.closed, gate.organization_id) == (
            "twin",
            world.session,
            world.candidate,
            False,
            world.world.org.id,
        )
        assert policy.read_gate(conn, "twin", world.session) == gate  # registered with the Guardian
        assert twin_subject.read_gate(conn, UUID(int=5)) is None
    with engine.begin() as c:
        c.execute(
            text("UPDATE twin_sessions SET status = 'closed', closed_at = now() WHERE id = :s"), {"s": world.session}
        )
    with db.transaction(world.candidate, role="worker") as conn:
        assert twin_subject.read_gate(conn, world.session).closed is True


# ---- events ----------------------------------------------------------------------------------------------------------


def test_a_rejection_a_release_an_expiry_and_a_cancel_each_wake_the_thread_once(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    rejected, released, expired, cancelled = (propose(engine, world) for _ in range(4))
    with db.transaction(world.candidate) as conn:
        guardian.decide(
            conn, world.candidate, rejected.approval_id, approve=False, payload_hash=_hash(engine, rejected)
        )
        guardian.decide(conn, world.candidate, released.approval_id, approve=True, payload_hash=_hash(engine, released))
    assert resumes(engine) == [f"a{rejected.action_id}.rejected"]
    assert guardian.release(released.approval_id, None) == "scheduled"
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :a"),
            {"a": expired.approval_id},
        )
    with db.transaction(role="worker") as conn:
        assert approvals.expire_due(conn) == 1
    with db.transaction(role="worker") as conn:
        report = guardian.cancel_open(conn, None, "session_closed", session_id=world.session)
    assert report["actions_cancelled"] == 2 and report["approvals_expired"] == 1
    # the released action was scheduled, so closing the session cancels it as well as the one still waiting
    assert sorted(resumes(engine)) == sorted(
        [
            f"a{rejected.action_id}.rejected",
            f"a{released.action_id}.scheduled",
            f"a{expired.action_id}.expired",
            f"a{cancelled.action_id}.cancelled",
            f"a{released.action_id}.cancelled",
        ]
    )
    payloads = w.rows(engine, "SELECT payload FROM queue_jobs WHERE kind = 'twin.resume' ORDER BY id")
    for row in payloads:
        assert set(row.payload) == {"question_id", "cause", "agent_action_id", "status"}


def _hash(engine: Engine, proposed: guardian.Proposed) -> str:
    return guardian.payload_hash(w.one(engine, "SELECT payload FROM agent_actions WHERE id = :a", a=proposed.action_id))


def test_an_edited_approval_wakes_the_thread_only_through_its_replacement(engine, clean):  # noqa: F811
    world = w.build(engine, dial="tell_me_after", topics={"experience": "answer"})
    relay = propose(engine, world, kind="relay", sentences=[])
    with db.transaction(world.candidate) as conn:
        guardian.decide(
            conn,
            world.candidate,
            relay.approval_id,
            approve=True,
            payload_hash=_hash(engine, relay),
            edits={"body": "Yes, from March."},
        )
    assert resumes(engine) == []  # the edited rejection does not wake the thread
    assert guardian.release(relay.approval_id, {"body": "Yes, from March."}) == "scheduled"
    (replacement,) = w.rows(
        engine, "SELECT id, inputs, payload FROM agent_actions WHERE inputs ->> 'replaces' IS NOT NULL"
    )
    assert resumes(engine) == [f"a{replacement.id}.scheduled"]
    assert replacement.payload["body"] == "Yes, from March."
    assert (
        replacement.inputs["question_id"]
        == w.rows(engine, "SELECT payload ->> 'question_id' AS q FROM agent_actions WHERE id = :a", a=relay.action_id)[
            0
        ].q
    )
    assert replacement.inputs["kind"] == "relay" and replacement.inputs["replaces"] == relay.action_id


def test_a_drafted_answer_cannot_be_edited(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    drafted = propose(engine, world)
    with db.transaction(world.candidate) as conn, pytest.raises(ApiError) as refused:
        guardian.decide(
            conn,
            world.candidate,
            drafted.approval_id,
            approve=True,
            payload_hash=_hash(engine, drafted),
            edits={"body": "Changed."},
        )
    assert refused.value.status == 422
    with db.transaction(world.candidate) as conn:
        assert guardian.approval_view(conn, world.candidate, drafted.approval_id)["editable_fields"] == []


def test_a_reply_edit_on_a_relay_must_be_a_body_and_nothing_else():
    payload = {"kind": "relay", "body": None}
    assert approvals.apply_edits("twin_answer", payload, {"body": " Yes. "})[0]["body"] == "Yes."
    for bad in ({"topic": "skills"}, {"body": ""}, {"body": 5}):
        with pytest.raises(ApiError):
            approvals.apply_edits("twin_answer", payload, bad)


def test_a_session_close_cancels_only_that_sessions_twin_actions(engine, clean):  # noqa: F811
    one, two = (w.build(engine, dial="ask_first", topics={"experience": "answer"}) for _ in range(2))
    mine, theirs = propose(engine, one), propose(engine, two)
    with db.transaction(role="worker") as conn:
        guardian.cancel_open(conn, None, "session_closed", session_id=one.session)
    status = {
        a: w.one(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=a)
        for a in (mine.action_id, theirs.action_id)
    }
    assert status == {mine.action_id: "cancelled", theirs.action_id: "awaiting_approval"}
    with db.transaction(role="worker") as conn, pytest.raises(ValueError):
        guardian.cancel_open(conn, None, "x")  # exactly one of campaign and session


def test_the_inputs_are_stored_with_the_action(engine, clean):  # noqa: F811
    world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
    question = _question(engine, world)
    proposed = propose(engine, world, question=question)
    inputs = w.one(engine, "SELECT inputs FROM agent_actions WHERE id = :a", a=proposed.action_id)
    assert inputs["question_id"] == str(question) and inputs["kind"] == "answer" and "provenance" in inputs
    with db.transaction(world.candidate, role="worker") as conn:  # the same run, question and kind: the same action
        run = conn.execute(
            text("SELECT agent_run_id FROM agent_actions WHERE id = :a"), {"a": proposed.action_id}
        ).scalar_one()
        again = guardian.propose_action(
            conn,
            run,
            action_type="twin_answer",
            payload={"kind": "answer"},
            provenance={},
            inputs={"question_id": str(question), "kind": "answer"},
        )
    assert again.action_id == proposed.action_id and again.status == "awaiting_approval"


# ---- the budget -----------------------------------------------------------------------------------------------------


def test_the_monthly_sum_leaves_out_twin_runs(engine, clean):  # noqa: F811
    world = w.build(engine)
    w.add_spend(engine, world, 400_000)  # a Twin run's call for this candidate
    with engine.connect() as c:
        month = budget.current_month()
        assert budget.spent_microusd(c, user_id=world.candidate, month=month) == 400_000
        assert budget.spent_microusd(c, user_id=world.candidate, month=month, leave_out_twin=True) == 0
        assert budget.session_spent_microusd(c, world.session) == 400_000
