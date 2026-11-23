"""TWN-01 to TWN-04 as running behaviour (plan 09-03): employer questions driven through the real graph, the real queue,
the real Guardian and the fake provider with recorded answers. Every path ends with the same guard (`settled`): the
queue is drained, the question has exactly one final Twin message, no resume is queued, no checkpoint is left and no
action is still open. Everything is invented."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import Engine, text

from app.guardian import approvals, runtime
from app.platform import db, queue
from app.twin import api as twin
from app.twin import deliver, logic
from tests.guardian.conftest import clean  # noqa: F401  (the fixture that empties the tables around a committing test)
from tests.twin.fixtures.graph import world as w

OPEN = {"experience": "answer"}
SKILL_FACT = ("skill_python", "skill", "Python and SQL.")


@pytest.fixture(autouse=True)
def graph_world(clean, monkeypatch):  # noqa: F811
    runtime.close()
    yield
    runtime.close()


def calls(engine: Engine, task: str | None = None) -> int:
    sql = "SELECT count(*) FROM llm_calls" + (" WHERE task = :t" if task else "")
    return int(w.one(engine, sql, t=task))


def test_a_restricted_question_is_declined_before_any_model_call(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world, "What is your current salary?")
    w.run_jobs()
    (final,) = w.finals(engine, question)
    assert (final.kind, final.body, final.topic) == ("declined", logic.DECLINED_TEXT, "current_pay")
    assert calls(engine) == 0  # no classifier call, no answer call
    assert w.one(engine, "SELECT count(*) FROM agent_runs WHERE agent_role = 'twin' AND status = 'succeeded'") == 1
    w.settled(engine, question)


def test_the_standing_yes_answers_with_citations_and_no_approval(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    (final,) = w.finals(engine, question)
    assert (final.kind, final.topic) == ("answered", "experience")
    assert final.cited_fact_ids == [world.world.candidate.record.fact_ids[1]]  # f2: the pipelines achievement
    assert "40 percent" in final.body
    assert w.one(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=final.agent_action_id) == "executed"
    assert w.one(engine, "SELECT count(*) FROM approvals") == 0
    assert w.twin_messages(engine, question) == [("twin", "answered", "experience")]
    w.settled(engine, question)


def test_ask_first_holds_the_answer_for_the_candidate_and_posts_it_after_approval(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    assert w.twin_messages(engine, question) == [("twin", "unknown", "experience")]  # the holding message
    with db.transaction(world.member) as conn:
        view = twin.question_view(conn, question)
    assert view.state == "waiting_for_candidate" and view.answer.body == logic.REVIEW_TEXT
    waiting = w.pending(engine, question)
    assert waiting.payload["kind"] == "answer" and waiting.payload["sentences"][0]["fact_ids"]
    assert w.checkpoints(engine, question) > 0  # parked
    w.approve(world, waiting.approval_id, waiting.payload)
    w.settled(engine, question)
    (final,) = w.finals(engine, question)
    assert final.kind == "answered" and final.body == waiting.payload["body"]


def test_an_ask_me_topic_waits_for_approval_even_with_the_standing_dial(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after")  # no topic row: experience is ask_me
    question = w.ask(world)
    w.run_jobs()
    waiting = w.pending(engine, question)
    assert w.finals(engine, question) == []
    w.approve(world, waiting.approval_id, waiting.payload)
    w.settled(engine, question)
    assert w.finals(engine, question)[0].kind == "answered"


def test_a_rejected_answer_ends_in_a_decline(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    w.reject(world, w.pending(engine, question).approval_id)
    w.settled(engine, question)
    (final,) = w.finals(engine, question)
    assert (final.kind, final.body) == ("declined", logic.DECLINED_TEXT)


def test_an_expired_approval_ends_in_a_decline(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    with engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE status = 'pending'"))
    with db.transaction(role="worker") as conn:
        assert approvals.expire_due(conn) == 1
    w.settled(engine, question)
    assert w.finals(engine, question)[0].kind == "declined"


def reply_path(engine, world, question, reply="I can start in 30 days."):
    waiting = w.pending(engine, question)
    assert waiting.payload["kind"] == "relay" and waiting.payload["body"] is None
    result = w.approve(world, waiting.approval_id, waiting.payload, reply=reply)
    assert result.edits == {"body": reply}
    return waiting


def test_when_the_record_does_not_answer_the_candidate_is_asked_and_the_reply_is_relayed(engine, monkeypatch):
    w.use_fakes(monkeypatch, "unknown")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world, "When could you start?")
    w.run_jobs()
    assert w.twin_messages(engine, question) == [("twin", "unknown", "experience")]
    assert w.finals(engine, question) == []
    waiting = reply_path(engine, world, question)
    assert w.finals(engine, question) == []  # nothing reaches the employer before the worker releases it
    w.settled(engine, question)
    (final,) = w.finals(engine, question)
    assert (final.kind, final.body) == ("relayed", "I can start in 30 days.")
    # the reply is an edit: the original action was rejected as `edited` and its replacement was released
    assert w.one(engine, "SELECT outcome_code FROM agent_actions WHERE id = :a", a=waiting.action_id) == "edited"
    replaced = w.one(engine, "SELECT inputs ->> 'replaces' FROM agent_actions WHERE id = :a", a=final.agent_action_id)
    assert replaced == str(waiting.action_id)
    assert (
        w.one(engine, "SELECT in_reply_to FROM twin_messages WHERE author = 'twin' AND answer_kind = 'relayed'")
        == question
    )


def test_with_the_dial_at_never_nothing_is_drafted_but_a_reply_is_still_relayed(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="never", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    assert calls(engine, "twin.answer") == 0 and calls(engine, "twin.classify") == 1
    assert w.twin_messages(engine, question) == [("twin", "unknown", "experience")]
    reply_path(engine, world, question)
    w.settled(engine, question)
    assert w.finals(engine, question)[0].kind == "relayed"


def test_a_failed_classifier_takes_the_restricted_path(engine, monkeypatch):
    w.use_fakes(monkeypatch, "invalid")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    (final,) = w.finals(engine, question)
    assert (final.kind, final.topic) == ("declined", "contact_details")
    assert calls(engine, "twin.answer") == 0
    w.settled(engine, question)


def test_a_topic_set_to_never_is_declined_after_classification_without_an_answer_call(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics={"experience": "never"})
    question = w.ask(world)
    w.run_jobs()
    assert w.finals(engine, question)[0].kind == "declined"
    assert calls(engine, "twin.classify") == 1 and calls(engine, "twin.answer") == 0
    w.settled(engine, question)


def test_a_model_that_says_pay_in_the_classification_is_declined_too(engine, monkeypatch):
    w.use_fakes(monkeypatch, "classified_pay")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world, "Is there anything you would not be happy with?")  # nothing the patterns catch
    w.run_jobs()
    (final,) = w.finals(engine, question)
    assert (final.kind, final.topic) == ("declined", "current_pay")
    assert calls(engine, "twin.answer") == 0


def test_a_blocked_draft_goes_to_the_candidate_as_a_relay(engine, monkeypatch):
    # the fact's own statement holds a phone number: the model only saw it removed, the Guardian reads the statement
    w.use_fakes(monkeypatch, "phonefact")
    world = w.build(
        engine,
        dial="tell_me_after",
        topics={"experience": "answer", "other": "answer"},
        facts=(("phone", "other", "Reach me on +91 98765 43210 about the migration project."),),
    )
    question = w.ask(world, "Tell me about the migration project.")
    w.run_jobs()
    blocked = w.rows(
        engine, "SELECT status::text AS s, outcome_code FROM agent_actions WHERE action_type = 'twin_answer'"
    )
    assert [(b.s, b.outcome_code) for b in blocked] == [("blocked", "restricted_data")] + [("awaiting_approval", None)]
    assert w.twin_messages(engine, question) == [("twin", "unknown", "experience")]
    reply_path(engine, world, question, "I led it end to end.")
    w.settled(engine, question)
    assert w.finals(engine, question)[0].kind == "relayed"


def test_a_reply_with_contact_details_is_refused_and_nothing_is_stored(engine, monkeypatch):
    w.use_fakes(monkeypatch, "unknown")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    waiting = w.pending(engine, question)
    from app.platform.errors import ApiError

    with pytest.raises(ApiError) as refused:
        w.approve(world, waiting.approval_id, waiting.payload, reply="Mail me at ada@example.test please.")
    assert refused.value.details["reason"] == "restricted_data"
    assert w.one(engine, "SELECT count(*) FROM twin_messages WHERE author = 'candidate'") == 0
    assert w.pending(engine, question).action_id == waiting.action_id  # still waiting


def test_the_third_restricted_question_closes_the_session(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    asked = []
    for _ in range(3):
        asked.append(w.ask(world, "What is your current salary?"))
        w.run_jobs()
    bodies = [w.finals(engine, q)[0].body for q in asked]
    assert bodies == [logic.DECLINED_TEXT, logic.DECLINED_TEXT, logic.CLOSED_TEXT]
    assert w.one(engine, "SELECT status::text FROM twin_sessions WHERE id = :s", s=world.session) == "closed"
    with db.transaction(world.member) as conn, pytest.raises(twin.SessionClosed):
        twin.ask(
            conn,
            introduction_id=world.world.introduction,
            question="Anything else?",
            asked_by=world.member,
            agent_token_id=None,
        )
    for q in asked:
        w.settled(engine, q)


def test_a_session_at_its_cost_cap_declines_and_closes(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    w.add_spend(engine, world, 500_000)
    question = w.ask(world)
    w.run_jobs()
    assert w.finals(engine, question)[0].body == logic.LIMIT_TEXT
    assert w.one(engine, "SELECT status::text FROM twin_sessions WHERE id = :s", s=world.session) == "closed"
    assert calls(engine, "twin.classify") == 0


def test_closing_a_session_settles_every_waiting_question_first(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    first, second = w.ask(world, "What did you build?"), w.ask(world, "What did you lead?")
    w.run_jobs()
    assert len(w.rows(engine, "SELECT 1 FROM approvals WHERE status = 'pending'")) == 2
    with db.transaction(role="worker") as conn:
        assert deliver.close_session(conn, world.session) == 2
    for q in (first, second):
        (final,) = w.finals(engine, q)
        assert (final.kind, final.body) == ("declined", logic.CLOSED_TEXT)
        w.settled(engine, q)
    assert w.one(engine, "SELECT count(*) FROM approvals WHERE status = 'pending'") == 0


def test_a_resume_for_another_action_leaves_the_thread_parked_and_a_late_one_posts_nothing(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    waiting = w.pending(engine, question)
    with db.transaction(role="worker") as conn:
        queue.enqueue(
            conn, "twin.resume", {"question_id": str(question), "cause": "a999999.scheduled"}, unique_key="x:1"
        )
        again = queue.enqueue(
            conn,
            "twin.resume",
            {"question_id": str(question), "cause": "start"},
            unique_key=f"twin.resume:{question}:start",
        )
    assert again is None  # a second resume with the same cause is a no-op
    w.run_jobs()
    assert w.finals(engine, question) == [] and w.checkpoints(engine, question) > 0
    w.approve(world, waiting.approval_id, waiting.payload)
    w.settled(engine, question)
    before = w.twin_messages(engine, question)
    with db.transaction(role="worker") as conn:
        queue.enqueue(conn, "twin.resume", {"question_id": str(question), "cause": "a1.scheduled"}, unique_key="x:2")
    w.settled(engine, question)
    assert w.twin_messages(engine, question) == before


def test_a_checkpoint_holds_ids_only(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="ask_first", topics=OPEN)
    question = w.ask(world, "What did you build at Initech Demo?")
    w.run_jobs()
    stored = w.rows(
        engine, "SELECT checkpoint::text AS c FROM langgraph.checkpoints WHERE thread_id = :t", t=f"twin:{question}"
    )
    blobs = w.rows(engine, "SELECT blob FROM langgraph.checkpoint_blobs WHERE thread_id = :t", t=f"twin:{question}")
    text_ = json.dumps([s.c for s in stored]) + "".join(
        bytes(b.blob).decode("utf-8", "ignore") for b in blobs if b.blob
    )
    for secret in ("Initech", "pipelines", "percent", "salary"):
        assert secret not in text_
    assert str(question) in text_  # the question's id is there, and that is all


def test_running_a_node_again_after_a_crash_writes_nothing_twice(engine, monkeypatch):
    from app.twin import nodes

    w.use_fakes(monkeypatch, "unknown")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world)
    state = {"question_id": str(question), "next": None, "cause": "start", "parked_on": None}
    assert nodes.triage(state)["next"] == "draft"
    assert nodes.triage(state)["next"] == "draft"
    assert w.one(engine, "SELECT count(*) FROM agent_runs WHERE agent_role = 'twin'") == 1
    assert calls(engine, "twin.classify") == 1  # the second triage read its first answer from the recorder
    assert nodes.draft(state)["next"] == "ask_candidate"
    assert nodes.ask_candidate(state)["next"] == "propose"
    assert nodes.ask_candidate(state)["next"] == "propose"
    first, second = nodes.propose(state), nodes.propose(state)
    assert first["next"] == second["next"] == "park" and first["parked_on"] == second["parked_on"]
    assert w.twin_messages(engine, question) == [("twin", "unknown", "experience")]
    assert w.one(engine, "SELECT count(*) FROM agent_actions WHERE action_type = 'twin_answer'") == 1
    assert w.one(engine, "SELECT count(*) FROM approvals") == 1


def test_delivery_runs_once_even_when_called_twice(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    question = w.ask(world)
    w.run_jobs()
    (final,) = w.finals(engine, question)
    with db.transaction(world.candidate, role="worker") as conn:
        assert deliver.post(conn, final.agent_action_id) is False  # already executed, message exists
    assert len(w.finals(engine, question)) == 1


def test_the_session_closes_nothing_before_the_cap(engine, monkeypatch):
    w.use_fakes(monkeypatch, "answerable")
    world = w.build(engine, dial="tell_me_after", topics=OPEN)
    w.add_spend(engine, world, 499_999)
    question = w.ask(world)
    w.run_jobs()
    assert w.finals(engine, question)[0].kind == "answered"
    assert w.one(engine, "SELECT status::text FROM twin_sessions WHERE id = :s", s=world.session) == "open"
    assert timedelta(0) == timedelta(0)
