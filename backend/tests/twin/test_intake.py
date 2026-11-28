"""The question intake and view of app.twin.api, and hiring.api.introduction_labels (plan 09-01; TWN-01, NET-01).

An employer member or an organization's token asks inside an accepted introduction only; the question is stored without
a topic and queued once for the Twin; the candidate and the employer each read it back as row-level security allows.
Tests run in the `conn` fixture's transaction, acting as the real `app_api` role with the request context a route or
the MCP gate would set; the Twin's own messages are inserted as the worker would. The last test runs the intake with
the worker's and the match module's database URLs unreachable, as the API container is. Everything is invented.
"""

import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text

from app.hiring import api as hiring
from app.platform import config, db
from app.twin import api as twin
from tests.campaigns.factories import make_action, make_approval, make_run
from tests.conftest import _empty_tables
from tests.hiring.factories import make_ask, make_candidate
from tests.twin.factories import (
    TwinWorld,
    employer_question,
    org_token,
    twin_session,
    twin_world,
    withdraw,
)

QUESTION = "What did you build at your last job?"


def act_as(
    conn: Connection, role: str, user: UUID | None = None, *, token: UUID | None = None, scopes: str = ""
) -> None:
    conn.execute(text("RESET ROLE"))
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if user else ""})
    conn.execute(text("SELECT set_config('app.agent_token_id', :t, true)"), {"t": str(token) if token else ""})
    conn.execute(text("SELECT set_config('app.token_scopes', :s, true)"), {"s": scopes})


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


@pytest.fixture
def world(conn) -> TwinWorld:
    return twin_world(conn)


def ask_as_member(conn: Connection, world: TwinWorld, introduction=None, question: str = QUESTION) -> twin.Asked:
    act_as(conn, "app_api", world.member.user)
    return twin.ask(
        conn,
        introduction_id=introduction or world.introduction,
        question=question,
        asked_by=world.member.user,
        agent_token_id=None,
    )


def jobs(conn: Connection) -> list:
    conn.execute(text("RESET ROLE"))
    return conn.execute(
        text(
            "SELECT kind, queue, unique_key, max_attempts, user_id, payload FROM queue_jobs WHERE kind = 'twin.resume'"
        )
    ).all()


# ---- ask -----------------------------------------------------------------------------------------------------------


def test_a_member_opens_a_session_stores_the_question_without_a_topic_and_queues_one_job(conn, world):
    asked = ask_as_member(conn, world)
    conn.execute(text("RESET ROLE"))
    session = conn.execute(
        text("SELECT user_id, organization_id, opened_by, agent_token_id, status::text FROM twin_sessions")
    ).one()
    assert session == (world.candidate.id, world.org.id, world.member.user, None, "open")
    message = conn.execute(
        text("SELECT session_id, author::text, body, topic, answer_kind, in_reply_to FROM twin_messages WHERE id = :q"),
        {"q": asked.question_id},
    ).one()
    assert message == (asked.session_id, "employer", QUESTION, None, None, None)
    (job,) = jobs(conn)
    assert job.queue == "interactive" and job.max_attempts == 20 and job.user_id == world.candidate.id
    assert job.unique_key == f"twin.resume:{asked.question_id}:start"
    assert job.payload == {"question_id": str(asked.question_id), "cause": "start"}


def test_a_second_question_reuses_the_session(conn, world):
    first = ask_as_member(conn, world)
    second = ask_as_member(conn, world, question="And what would you change about it?")
    assert first.session_id == second.session_id and first.question_id != second.question_id
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_sessions") == 1
    assert len(jobs(conn)) == 2


def test_the_question_is_trimmed_and_limited_before_anything_is_written(conn, world):
    for bad in ("", "    ", "Hi?", "x" * (twin.QUESTION_MAX + 1)):
        with pytest.raises(twin.InvalidQuestion):
            ask_as_member(conn, world, question=bad)
    ask_as_member(conn, world, question="  " + "x" * twin.QUESTION_MIN + "  ")
    ask_as_member(conn, world, question="x" * twin.QUESTION_MAX)
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_messages") == 2
    assert one(conn, "SELECT min(length(body)) FROM twin_messages") == twin.QUESTION_MIN


def test_an_organization_token_with_twin_ask_asks_and_its_id_is_recorded_on_the_session(conn, world):
    conn.execute(text("RESET ROLE"))
    token = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    act_as(conn, "app_api", world.org.owner, token=token.id, scopes="twin:ask")
    asked = twin.ask(
        conn, introduction_id=world.introduction, question=QUESTION, asked_by=world.org.owner, agent_token_id=token.id
    )
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT agent_token_id FROM twin_sessions WHERE id = :s", s=asked.session_id) == token.id
    assert one(conn, "SELECT opened_by FROM twin_sessions WHERE id = :s", s=asked.session_id) == world.org.owner
    assert len(jobs(conn)) == 1


def test_a_token_without_twin_ask_is_told_there_is_no_such_introduction_and_nothing_is_written(conn, world):
    conn.execute(text("RESET ROLE"))
    token = org_token(conn, world.org, world.org.owner, ["introductions:request"])
    act_as(conn, "app_api", world.org.owner, token=token.id, scopes="introductions:request")
    with pytest.raises(twin.IntroductionNotFound):
        twin.ask(
            conn,
            introduction_id=world.introduction,
            question=QUESTION,
            asked_by=world.org.owner,
            agent_token_id=token.id,
        )
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_sessions") == 0 and jobs(conn) == []


@pytest.mark.parametrize("status", ["asked", "declined", "expired"])
def test_an_introduction_that_is_not_accepted_is_not_found(conn, world, status):
    ask = make_ask(conn, world.role, make_candidate(conn))
    if status != "asked":
        conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        conn.execute(
            text(
                "UPDATE introductions SET status = CAST(:s AS introduction_status), "
                "responded_at = CASE WHEN :s = 'declined' THEN now() END WHERE id = :i"
            ),
            {"s": status, "i": ask.id},
        )
        conn.execute(text("SELECT set_config('app.maintenance', '', true)"))
    with pytest.raises(twin.IntroductionNotFound):
        ask_as_member(conn, world, introduction=ask.id)


def test_another_organizations_introduction_an_unknown_one_and_the_candidates_own_are_not_found(conn, world):
    elsewhere = twin_world(conn)
    with pytest.raises(twin.IntroductionNotFound):
        ask_as_member(conn, world, introduction=elsewhere.introduction)
    with pytest.raises(twin.IntroductionNotFound):
        ask_as_member(conn, world, introduction=uuid4())
    act_as(conn, "app_api", world.candidate.id)  # the candidate does not ask their own Twin
    with pytest.raises(twin.IntroductionNotFound):
        twin.ask(
            conn,
            introduction_id=world.introduction,
            question=QUESTION,
            asked_by=world.candidate.id,
            agent_token_id=None,
        )
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_sessions") == 0 and jobs(conn) == []


def test_a_closed_session_takes_no_question(conn, world):
    twin_session(conn, world, status="closed")
    with pytest.raises(twin.SessionClosed):
        ask_as_member(conn, world)
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_messages") == 0 and jobs(conn) == []


def test_the_forty_first_question_is_refused(conn, world):
    session = twin_session(conn, world)
    for number in range(twin.SESSION_QUESTIONS):
        employer_question(conn, session, f"Invented question number {number}?")
    with pytest.raises(twin.LimitReached):
        ask_as_member(conn, world)
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_messages") == twin.SESSION_QUESTIONS and jobs(conn) == []


def test_withdrawn_consent_stops_questions_with_or_without_a_session(conn, world):
    withdraw(conn, world.accepted.consent)
    with pytest.raises(twin.ConsentMissing):  # no session yet: the session check refuses
        ask_as_member(conn, world)
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT count(*) FROM twin_sessions") == 0
    other = twin_world(conn)
    twin_session(conn, other)
    withdraw(conn, other.accepted.consent)
    act_as(conn, "app_api", other.member.user)
    with pytest.raises(twin.ConsentMissing):  # a session exists: the message check refuses
        twin.ask(
            conn, introduction_id=other.introduction, question=QUESTION, asked_by=other.member.user, agent_token_id=None
        )
    conn.execute(text("RESET ROLE"))
    assert jobs(conn) == []


def test_the_connection_stays_usable_after_a_refusal(conn, world):
    with pytest.raises(twin.IntroductionNotFound):
        ask_as_member(conn, world, introduction=uuid4())
    assert ask_as_member(conn, world).question_id  # the same transaction goes on


# ---- question_view -------------------------------------------------------------------------------------------------


def twin_message(conn, world, session, question, kind, *, topic="skills", body="Invented.", cited=(), action=None):
    conn.execute(text("RESET ROLE"))
    return one(
        conn,
        """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                      cited_fact_ids, in_reply_to, agent_action_id)
           VALUES (:s, :u, :o, 'twin', :b, CAST(:t AS twin_topic), CAST(:k AS twin_answer_kind), CAST(:c AS uuid[]),
                   :q, :a) RETURNING id""",
        s=session,
        u=world.candidate.id,
        o=world.org.id,
        b=body,
        t=topic,
        k=kind,
        c=list(cited),
        q=question,
        a=action,
    )


def executed_action(conn, world, kind="answer", question=None):
    conn.execute(text("RESET ROLE"))
    run = make_run(conn, world.candidate.id, None, "twin")
    return make_action(
        conn,
        run,
        action_type="twin_answer",
        status="executed",
        payload={"kind": kind, "question_id": str(question)},
        policy_decision="ask",
    )


def test_a_fresh_question_is_pending_with_no_topic_and_no_answer(conn, world):
    asked = ask_as_member(conn, world)
    for user in (world.member.user, world.candidate.id):
        act_as(conn, "app_api", user)
        view = twin.question_view(conn, asked.question_id)
        assert view is not None
        assert (view.state, view.topic, view.answer) == ("pending", None, None)
        assert view.question.body == QUESTION
        assert (view.session_id, view.introduction_id) == (asked.session_id, world.introduction)


def test_a_holding_message_means_waiting_for_candidate_and_a_decline_means_declined(conn, world):
    asked = ask_as_member(conn, world)
    twin_message(conn, world, asked.session_id, asked.question_id, "unknown", body="The candidate reviews answers.")
    act_as(conn, "app_api", world.member.user)
    view = twin.question_view(conn, asked.question_id)
    assert view is not None and view.state == "waiting_for_candidate" and view.topic == "skills"
    assert view.answer is not None and view.answer.answer_kind == "unknown" and "reviews" in view.answer.body
    assert view.answer.citations == () and view.answer.author == "twin"
    twin_message(conn, world, asked.session_id, asked.question_id, "declined", body="Not shared.")
    act_as(conn, "app_api", world.member.user)
    view = twin.question_view(conn, asked.question_id)
    assert view is not None and view.state == "declined" and view.answer is not None
    assert view.answer.body == "Not shared."  # the final message wins over the older holding one


def test_an_answer_carries_its_citations_in_order_for_the_employer_and_the_candidate(conn, world):
    asked = ask_as_member(conn, world)
    facts = world.candidate.record.fact_ids
    action = executed_action(conn, world, question=asked.question_id)
    twin_message(
        conn, world, asked.session_id, asked.question_id, "answered", cited=[facts[1], facts[0]], action=action
    )
    statements = [one(conn, "SELECT statement FROM profile_facts WHERE id = :f", f=f) for f in (facts[1], facts[0])]
    for user in (world.member.user, world.candidate.id):
        act_as(conn, "app_api", user)
        view = twin.question_view(conn, asked.question_id)
        assert view is not None and view.state == "answered"
        assert view.answer is not None
        assert [(c.fact_id, c.quote) for c in view.answer.citations] == list(
            zip((facts[1], facts[0]), statements, strict=True)
        )


def test_the_employer_of_another_organization_and_a_token_without_twin_ask_get_none(conn, world):
    asked = ask_as_member(conn, world)
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_api", twin_world(conn).member.user)
    assert twin.question_view(conn, asked.question_id) is None
    conn.execute(text("RESET ROLE"))
    token = org_token(conn, world.org, world.org.owner, ["introductions:request"])
    act_as(conn, "app_api", world.org.owner, token=token.id, scopes="introductions:request")
    assert twin.question_view(conn, asked.question_id) is None
    assert twin.question_view(conn, uuid4()) is None
    conn.execute(text("RESET ROLE"))
    asker = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    act_as(conn, "app_api", world.org.owner, token=asker.id, scopes="twin:ask")
    assert twin.question_view(conn, asked.question_id) is not None


def test_the_candidate_sees_the_pending_approval_whether_a_reply_is_wanted_and_their_reply(conn, world):
    asked = ask_as_member(conn, world)
    twin_message(conn, world, asked.session_id, asked.question_id, "unknown", body="The candidate is asked.")
    run = make_run(conn, world.candidate.id, None, "twin")
    action = make_action(
        conn,
        run,
        action_type="twin_answer",
        status="proposed",
        payload={"kind": "relay", "question_id": str(asked.question_id), "body": None},
        policy_decision="ask",
    )
    approval = make_approval(conn, action)
    act_as(conn, "app_api", world.candidate.id)
    view = twin.question_view(conn, asked.question_id)
    assert view is not None
    assert (view.approval_id, view.needs_reply, view.reply) == (str(approval), True, None)
    act_as(conn, "app_api", world.member.user)  # the employer sees none of it
    employer = twin.question_view(conn, asked.question_id)
    assert employer is not None and (employer.approval_id, employer.needs_reply, employer.reply) == (None, False, None)
    conn.execute(text("RESET ROLE"))
    conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, in_reply_to)
               VALUES (:s, :u, :o, 'candidate', 'My own words.', :q)"""
        ),
        {"s": asked.session_id, "u": world.candidate.id, "o": world.org.id, "q": asked.question_id},
    )
    act_as(conn, "app_api", world.candidate.id)
    view = twin.question_view(conn, asked.question_id)
    assert view is not None and view.reply is not None and view.reply.body == "My own words."
    act_as(conn, "app_api", world.member.user)  # still hidden from the employer
    assert twin.question_view(conn, asked.question_id).reply is None  # type: ignore[union-attr]


def test_an_approval_for_an_answer_draft_does_not_ask_for_a_reply(conn, world):
    asked = ask_as_member(conn, world)
    twin_message(conn, world, asked.session_id, asked.question_id, "unknown")
    run = make_run(conn, world.candidate.id, None, "twin")
    action = make_action(
        conn,
        run,
        action_type="twin_answer",
        status="proposed",
        payload={"kind": "answer", "question_id": str(asked.question_id)},
        policy_decision="ask",
    )
    approval = make_approval(conn, action)
    act_as(conn, "app_api", world.candidate.id)
    view = twin.question_view(conn, asked.question_id)
    assert view is not None and (view.approval_id, view.needs_reply) == (str(approval), False)


# ---- wait ---------------------------------------------------------------------------------------------------------


def test_wait_returns_the_pending_view_after_the_time_and_at_once_for_a_settled_one(conn, world):
    asked = ask_as_member(conn, world)

    def read(question_id: UUID):
        return twin.question_view(conn, question_id)

    started = time.monotonic()
    act_as(conn, "app_api", world.member.user)
    view = twin.wait(asked.question_id, read, 0)
    assert view is not None and view.state == "pending" and time.monotonic() - started < 1
    twin_message(conn, world, asked.session_id, asked.question_id, "declined")
    act_as(conn, "app_api", world.member.user)
    started = time.monotonic()
    view = twin.wait(asked.question_id, read, 30)
    assert view is not None and view.state == "declined" and time.monotonic() - started < 1


def test_wait_polls_until_the_state_leaves_pending_and_returns_none_for_an_unseen_question(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(twin.time, "sleep", lambda seconds: slept.append(seconds))
    states = iter(["pending", "pending", "waiting_for_candidate"])
    base = twin.QuestionView(uuid4(), uuid4(), uuid4(), None, "pending", twin.QuestionBody("q", None), None)  # type: ignore[arg-type]
    calls: list[UUID] = []

    def read(question_id: UUID):
        calls.append(question_id)
        return replace(base, state=next(states))  # type: ignore[arg-type]

    view = twin.wait(base.question_id, read, 30)
    assert view is not None and view.state == "waiting_for_candidate"
    assert len(calls) == 3 and slept == [twin.POLL_SECONDS, twin.POLL_SECONDS]
    assert twin.wait(uuid4(), lambda _: None, 30) is None


# ---- hiring.api.introduction_labels --------------------------------------------------------------------------------


def test_labels_name_the_organization_and_role_for_each_callers_own_introductions(conn, world):
    pending = make_ask(conn, world.role, make_candidate(conn))
    other = twin_world(conn)
    wanted = [world.introduction, pending.id, other.introduction, uuid4()]
    expected = hiring.IntroductionLabel(world.org.id, "Halden Demo", world.role.id, world.role.title)
    act_as(conn, "app_api", world.candidate.id)  # the candidate: their own introduction only
    assert hiring.introduction_labels(conn, wanted) == {world.introduction: expected}
    act_as(conn, "app_api", world.member.user)  # a member: the accepted introductions of the organization
    assert hiring.introduction_labels(conn, wanted) == {world.introduction: expected}
    conn.execute(text("RESET ROLE"))
    token = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    act_as(conn, "app_api", world.org.owner, token=token.id, scopes="twin:ask")  # its agent: the same
    assert hiring.introduction_labels(conn, wanted) == {world.introduction: expected}
    act_as(conn, "app_api", make_candidate_outside(conn))
    assert hiring.introduction_labels(conn, wanted) == {}
    assert hiring.introduction_labels(conn, []) == {}


def make_candidate_outside(conn: Connection) -> UUID:
    conn.execute(text("RESET ROLE"))
    return make_candidate(conn).id


# ---- the API login only ---------------------------------------------------------------------------------------------

UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def api_login_only(engine, monkeypatch):
    _empty_tables(engine)
    current = config.get()
    changed = replace(current, database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make them again
    yield
    for made in db._engines.values():
        made.dispose()
    _empty_tables(engine)


def test_asking_reading_and_waiting_need_only_the_api_login(engine, api_login_only):
    """The API container holds only the API login. Asking, reading and waiting for an answer open no worker or match
    transaction: with those two URLs pointing at nothing, they still work, and only the API engine is ever made."""
    with engine.begin() as c:
        world = twin_world(c)

    def read(question_id: UUID):
        with db.transaction(world.member.user) as c:
            return twin.question_view(c, question_id)

    with db.transaction(world.member.user) as c:
        asked = twin.ask(
            c, introduction_id=world.introduction, question=QUESTION, asked_by=world.member.user, agent_token_id=None
        )
    view = twin.wait(asked.question_id, read, 0)
    assert view is not None and view.state == "pending"
    with db.transaction(world.candidate.id) as c:
        assert twin.question_view(c, asked.question_id) is not None
        assert hiring.introduction_labels(c, [world.introduction])
    assert set(db._engines) == {"api"}
    with engine.connect() as c:
        assert one(c, "SELECT count(*) FROM queue_jobs WHERE kind = 'twin.resume'") == 1


def test_no_route_reachable_code_of_the_intake_opens_a_worker_or_match_transaction():
    """Static half of the proof above: the intake and the hiring reader name neither role."""
    for path in ("app/twin/api.py", "app/twin/routes.py", "app/hiring/api.py", "app/agentnet/routes.py"):
        source = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")
        assert 'role="worker"' not in source and "role='worker'" not in source, path
        assert 'role="match"' not in source and "role='match'" not in source, path
