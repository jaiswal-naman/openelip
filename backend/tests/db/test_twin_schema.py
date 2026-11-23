"""Revision p9_0901_twin_agentnet, the Twin side (plan 09-01; TWN-01, TWN-03, TRU-03; DATA_MODEL 3.11, 5.5, T6).

The database refuses what the invariants forbid, whatever the application does: a Twin session only on an accepted
introduction with live consent; no pay or contact scope other than `never`, and no answer or relay on those topics;
an answer or a relay only as an executed twin_answer action; at most one final Twin message per question; an
append-only history. The guard amendment: a relay (the candidate's own reply) is not stopped by the dial `never` and
always needs an approved approval, and nothing else about the two keys changed. Then row-level security as the real
`app_api` role, and the grants.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back; rows come from
tests/twin/factories.py and are invented.
"""

import json
from contextlib import contextmanager
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.campaigns.factories import make_action, make_approval, make_check, make_run
from tests.hiring.factories import make_ask, make_candidate
from tests.match import factories as match
from tests.twin.factories import (
    TwinWorld,
    candidate_token,
    employer_question,
    org_token,
    twin_session,
    twin_world,
    withdraw,
)


@contextmanager
def refused(conn: Connection):
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def act_as(
    conn: Connection, role: str, user: UUID | None = None, *, token: UUID | None = None, scopes: str = ""
) -> None:
    """What a request is: a login role, the person, and for an outside agent its token and scopes."""
    conn.execute(text("RESET ROLE"))
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if user else ""})
    conn.execute(text("SELECT set_config('app.agent_token_id', :t, true)"), {"t": str(token) if token else ""})
    conn.execute(text("SELECT set_config('app.token_scopes', :s, true)"), {"s": scopes})


def as_bootstrap(conn: Connection) -> None:
    conn.execute(text("RESET ROLE"))


@pytest.fixture
def world(conn) -> TwinWorld:
    return twin_world(conn)


def message(
    conn, session, *, author, body="Invented text.", topic=None, kind=None, cited=(), reply_to=None, action=None
):
    return one(
        conn,
        """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                      cited_fact_ids, in_reply_to, agent_action_id)
           SELECT s.id, s.user_id, s.organization_id, CAST(:author AS twin_author), :body,
                  CAST(:topic AS twin_topic), CAST(:kind AS twin_answer_kind), CAST(:cited AS uuid[]), :reply,
                  :action
           FROM twin_sessions s WHERE s.id = :s RETURNING id""",
        s=session,
        author=author,
        body=body,
        topic=topic,
        kind=kind,
        cited=list(cited),
        reply=reply_to,
        action=action,
    )


def executed_action(conn, world: TwinWorld, kind: str = "answer") -> int:
    """An executed twin_answer of the candidate, as the worker leaves it (moved under app.maintenance: the keys
    themselves are tested further down and in test_two_key_schema.py)."""
    run = make_run(conn, world.candidate.id, None, "twin")
    return make_action(
        conn, run, action_type="twin_answer", status="executed", payload={"kind": kind}, policy_decision="ask"
    )


# ---- T6: the scope table ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["current_pay", "contact_details"])
@pytest.mark.parametrize("permission", ["answer", "ask_me"])
def test_pay_and_contact_scopes_are_never_anything_but_never(conn, world, topic, permission):
    with refused(conn) as caught:
        conn.execute(
            text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, :t, :p)"),
            {"u": world.candidate.id, "t": topic, "p": permission},
        )
    assert caught.value.orig.diag.constraint_name == "twin_scopes_never_pay_or_contact"


def test_pay_and_contact_may_be_recorded_as_never_and_other_topics_take_any_permission(conn, world):
    for topic, permission in (("current_pay", "never"), ("contact_details", "never"), ("skills", "answer")):
        conn.execute(
            text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, :t, :p)"),
            {"u": world.candidate.id, "t": topic, "p": permission},
        )
    assert one(conn, "SELECT count(*) FROM twin_scopes WHERE user_id = :u", u=world.candidate.id) == 3


# ---- sessions: consent first -----------------------------------------------------------------------------------


def test_a_session_opens_on_an_accepted_introduction_with_live_consent(conn, world):
    session = twin_session(conn, world)
    assert one(conn, "SELECT status::text FROM twin_sessions WHERE id = :s", s=session) == "open"


@pytest.mark.parametrize("status", ["asked", "declined", "expired"])
def test_no_session_on_an_introduction_that_is_not_accepted(conn, status):
    other = twin_world(conn)
    # a second candidate asked for the same role and not accepted (or answered otherwise)
    second = make_candidate(conn)
    ask = make_ask(conn, other.role, second)
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
    with refused(conn) as caught:
        conn.execute(
            text("INSERT INTO twin_sessions (introduction_id, user_id, organization_id) VALUES (:i, :u, :o)"),
            {"i": ask.id, "u": second.id, "o": other.org.id},
        )
    assert "consent_missing" in str(caught.value)


def test_no_session_for_another_organization_than_the_introductions(conn, world):
    elsewhere = twin_world(conn)
    with refused(conn) as caught:
        conn.execute(
            text("INSERT INTO twin_sessions (introduction_id, user_id, organization_id) VALUES (:i, :u, :o)"),
            {"i": world.introduction, "u": world.candidate.id, "o": elsewhere.org.id},
        )
    assert "consent_missing" in str(caught.value)


def test_no_session_once_the_introduction_consent_is_withdrawn(conn, world):
    withdraw(conn, world.accepted.consent)
    with refused(conn) as caught:
        twin_session(conn, world)
    assert "consent_missing" in str(caught.value)


# ---- messages --------------------------------------------------------------------------------------------------


@pytest.fixture
def asked(conn, world):
    """(session, question): an open session and the employer's question in it, which has no topic."""
    session = twin_session(conn, world)
    return session, employer_question(conn, session)


def test_an_employer_question_has_no_topic_and_a_twin_message_must_have_one(conn, world, asked):
    session, question = asked
    assert one(conn, "SELECT topic FROM twin_messages WHERE id = :m", m=question) is None
    with refused(conn) as caught:
        message(conn, session, author="twin", kind="unknown", reply_to=question)
    assert "twin_messages_check" in caught.value.orig.diag.constraint_name or "null value" in str(caught.value)


def test_an_answer_needs_cited_facts(conn, world, asked):
    session, question = asked
    action = executed_action(conn, world)
    with refused(conn):
        message(conn, session, author="twin", kind="answered", topic="experience", reply_to=question, action=action)


@pytest.mark.parametrize("flaw", ["unconfirmed", "rejected", "superseded", "another_user"])
def test_a_cited_fact_must_be_a_confirmed_current_fact_of_this_candidate(conn, world, asked, flaw):
    session, question = asked
    action = executed_action(conn, world)
    if flaw == "another_user":
        fact = make_candidate(conn).record.fact_ids[0]
    else:
        # a copy of the candidate's first fact, anchored the same way, in the wanted state
        fact = one(
            conn,
            """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote,
                                         confirmed_at, rejected_at, superseded_at)
               SELECT user_id, kind, statement || ' (copy)', source, document_id, source_span, source_quote,
                      CASE WHEN :f IN ('unconfirmed', 'rejected') THEN NULL ELSE now() END,
                      CASE WHEN :f = 'rejected' THEN now() END,
                      CASE WHEN :f = 'superseded' THEN now() END
               FROM profile_facts WHERE id = :base RETURNING id""",
            base=world.candidate.record.fact_ids[0],
            f=flaw,
        )
    with refused(conn) as caught:
        message(
            conn,
            session,
            author="twin",
            kind="answered",
            topic="skills",
            cited=[fact],
            reply_to=question,
            action=action,
        )
    assert "citation_unresolved" in str(caught.value)


def test_an_answer_needs_an_executed_twin_answer_action(conn, world, asked):
    session, question = asked
    fact = world.candidate.record.fact_ids[0]
    run = make_run(conn, world.candidate.id, None, "twin")
    for status in ("proposed", "scheduled"):
        action = make_action(
            conn, run, action_type="twin_answer", status=status, payload={"kind": "answer"}, policy_decision="ask"
        )
        with refused(conn) as caught:
            message(
                conn,
                session,
                author="twin",
                kind="answered",
                topic="skills",
                cited=[fact],
                reply_to=question,
                action=action,
            )
        assert "two_key" in str(caught.value)
    with refused(conn):  # no action at all
        message(conn, session, author="twin", kind="answered", topic="skills", cited=[fact], reply_to=question)
    other_type = make_action(conn, run, action_type="submit_application", status="executed", policy_decision="ask")
    with refused(conn) as caught:  # an executed action of another type is no key
        message(
            conn,
            session,
            author="twin",
            kind="answered",
            topic="skills",
            cited=[fact],
            reply_to=question,
            action=other_type,
        )
    assert "two_key" in str(caught.value)


def test_an_answer_on_an_ask_me_topic_is_accepted_with_an_executed_action(conn, world, asked):
    session, question = asked
    action = executed_action(conn, world)
    answer = message(
        conn,
        session,
        author="twin",
        kind="answered",
        topic="skills",
        cited=[world.candidate.record.fact_ids[0]],
        reply_to=question,
        action=action,
    )
    assert one(conn, "SELECT answer_kind::text FROM twin_messages WHERE id = :m", m=answer) == "answered"


@pytest.mark.parametrize("topic", ["current_pay", "contact_details"])
@pytest.mark.parametrize("kind", ["answered", "relayed"])
def test_pay_and_contact_are_never_answered_or_relayed(conn, world, asked, topic, kind):
    session, question = asked
    action = executed_action(conn, world, "relay" if kind == "relayed" else "answer")
    cited = [world.candidate.record.fact_ids[0]] if kind == "answered" else []
    with refused(conn) as caught:
        message(conn, session, author="twin", kind=kind, topic=topic, cited=cited, reply_to=question, action=action)
    assert "scope_denied" in str(caught.value)


def test_a_topic_the_candidate_closed_is_not_answered_and_not_relayed_but_may_be_declined(conn, world, asked):
    session, question = asked
    conn.execute(
        text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, 'skills', 'never')"),
        {"u": world.candidate.id},
    )
    fact = world.candidate.record.fact_ids[0]
    with refused(conn) as caught:
        message(
            conn,
            session,
            author="twin",
            kind="answered",
            topic="skills",
            cited=[fact],
            reply_to=question,
            action=executed_action(conn, world),
        )
    assert "scope_denied" in str(caught.value)
    with refused(conn) as caught:
        message(
            conn,
            session,
            author="twin",
            kind="relayed",
            topic="skills",
            reply_to=question,
            action=executed_action(conn, world, "relay"),
        )
    assert "scope_denied" in str(caught.value)
    message(conn, session, author="twin", kind="declined", topic="skills", reply_to=question)


def test_a_topic_open_for_answers_may_be_answered(conn, world, asked):
    session, question = asked
    conn.execute(
        text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, 'skills', 'answer')"),
        {"u": world.candidate.id},
    )
    message(
        conn,
        session,
        author="twin",
        kind="answered",
        topic="skills",
        cited=[world.candidate.record.fact_ids[0]],
        reply_to=question,
        action=executed_action(conn, world),
    )


def test_only_one_final_twin_message_per_question_but_holding_messages_may_repeat(conn, world, asked):
    session, question = asked
    fact = world.candidate.record.fact_ids[0]
    message(conn, session, author="twin", kind="unknown", topic="skills", reply_to=question)
    message(conn, session, author="twin", kind="unknown", topic="skills", reply_to=question, body="Again.")
    message(
        conn,
        session,
        author="twin",
        kind="answered",
        topic="skills",
        cited=[fact],
        reply_to=question,
        action=executed_action(conn, world),
    )
    for kind, action_kind in (("answered", "answer"), ("declined", None), ("relayed", "relay")):
        with refused(conn) as caught:
            message(
                conn,
                session,
                author="twin",
                kind=kind,
                topic="skills",
                reply_to=question,
                cited=[fact] if kind == "answered" else [],
                action=executed_action(conn, world, action_kind) if action_kind else None,
            )
        assert caught.value.orig.diag.constraint_name == "twin_messages_one_final", kind


def test_a_relay_needs_a_reply_target_in_its_own_session_and_an_executed_action(conn, world, asked):
    session, question = asked
    relay_action = executed_action(conn, world, "relay")
    with refused(conn):  # in_reply_to is required (CHECK)
        message(conn, session, author="twin", kind="relayed", topic="skills", action=relay_action)
    elsewhere_world = twin_world(conn)
    elsewhere_question = employer_question(conn, twin_session(conn, elsewhere_world))
    with refused(conn) as caught:  # the target lives in another session
        message(
            conn,
            session,
            author="twin",
            kind="relayed",
            topic="skills",
            reply_to=elsewhere_question,
            action=relay_action,
        )
    assert "reply is not in this session" in str(caught.value)
    with refused(conn):  # no action
        message(conn, session, author="twin", kind="relayed", topic="skills", reply_to=question)
    message(conn, session, author="twin", kind="relayed", topic="skills", reply_to=question, action=relay_action)


def test_only_a_twin_message_carries_an_action_or_cited_facts(conn, world, asked):
    session, _ = asked
    with refused(conn) as caught:
        message(conn, session, author="employer", action=executed_action(conn, world))
    assert "only a Twin message" in str(caught.value)
    with refused(conn) as caught:
        message(conn, session, author="employer", cited=[world.candidate.record.fact_ids[0]])
    assert "only a Twin message" in str(caught.value)


def test_a_closed_session_takes_no_message(conn, world):
    session = twin_session(conn, world, status="closed")
    with refused(conn) as caught:
        employer_question(conn, session)
    assert "closed" in str(caught.value)


def test_no_message_after_the_introduction_consent_is_withdrawn(conn, world, asked):
    session, question = asked
    withdraw(conn, world.accepted.consent)
    with refused(conn) as caught:
        employer_question(conn, session)
    assert "consent_missing" in str(caught.value)
    with refused(conn) as caught:  # the Twin's holding message too
        message(conn, session, author="twin", kind="unknown", topic="skills", reply_to=question)
    assert "consent_missing" in str(caught.value)


def test_messages_are_append_only_except_for_erasure(conn, world, asked):
    session, question = asked
    for sql in ("UPDATE twin_messages SET body = 'changed' WHERE id = :m", "DELETE FROM twin_messages WHERE id = :m"):
        with refused(conn) as caught:
            conn.execute(text(sql), {"m": question})
        assert "append-only" in str(caught.value)
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM twin_messages WHERE id = :m"), {"m": question})
    assert one(conn, "SELECT count(*) FROM twin_messages WHERE id = :m", m=question) == 0


def test_deleting_a_question_removes_its_checkpoint_thread(conn, world, asked):
    session, question = asked
    conn.execute(
        text(
            """INSERT INTO langgraph.checkpoints (thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata)
               VALUES (:t, '', 'c1', '{}', '{}'), ('twin:other', '', 'c1', '{}', '{}')"""
        ),
        {"t": f"twin:{question}"},
    )
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("DELETE FROM twin_messages WHERE id = :m"), {"m": question})
    assert one(conn, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t", t=f"twin:{question}") == 0
    assert one(conn, "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = 'twin:other'") == 1


# ---- the guard amendment: a relay under the dial never ---------------------------------------------------------


def proposed_twin_action(conn, world: TwinWorld, kind: str | None, policy: str = "ask") -> int:
    run = make_run(conn, world.candidate.id, None, "twin")
    payload = {"kind": kind} if kind else {"answer": "invented"}
    action = make_action(
        conn, run, action_type="twin_answer", status="proposed", payload=payload, policy_decision=policy
    )
    make_check(conn, action, "scope", True)
    return action


def set_dial(conn, user, level, action_type="twin_answer"):
    conn.execute(
        text("INSERT INTO autonomy_settings (user_id, action_type, level) VALUES (:u, :t, CAST(:l AS autonomy_level))"),
        {"u": user, "t": action_type, "l": level},
    )


def move(conn, action: int, status: str) -> None:
    conn.execute(
        text(
            """UPDATE agent_actions SET status = CAST(:s AS action_status),
                      executed_at = CASE WHEN :s = 'executed' THEN now() END WHERE id = :a"""
        ),
        {"a": action, "s": status},
    )


def test_a_relay_moves_under_the_dial_never_once_approved_and_not_before(conn, world):
    set_dial(conn, world.candidate.id, "never")
    relay = proposed_twin_action(conn, world, "relay")
    with refused(conn) as caught:
        move(conn, relay, "scheduled")
    assert "has not approved" in str(caught.value)
    make_approval(conn, relay, status="approved")
    move(conn, relay, "scheduled")
    move(conn, relay, "executed")
    assert one(conn, "SELECT status::text FROM agent_actions WHERE id = :a", a=relay) == "executed"


def test_an_answer_under_the_dial_never_is_still_refused_even_with_an_approval(conn, world):
    set_dial(conn, world.candidate.id, "never")
    for kind in ("answer", None):
        action = proposed_twin_action(conn, world, kind)
        make_approval(conn, action, status="approved")
        with refused(conn) as caught:
            move(conn, action, "scheduled")
        assert "to never" in str(caught.value) and "two_key" in str(caught.value), kind


def test_a_relay_needs_an_approval_even_when_the_policy_allows_and_the_dial_is_tell_me_after(conn, world):
    set_dial(conn, world.candidate.id, "tell_me_after")
    relay = proposed_twin_action(conn, world, "relay", policy="allow")
    with refused(conn) as caught:
        move(conn, relay, "scheduled")
    assert "has not approved" in str(caught.value)
    answer = proposed_twin_action(conn, world, "answer", policy="allow")  # the standing yes still needs no approval
    move(conn, answer, "scheduled")


def test_the_other_action_types_keep_their_two_keys_under_the_dial_never(conn, world):
    set_dial(conn, world.candidate.id, "never", "submit_application")
    run = make_run(conn, world.candidate.id, None, "operator")
    action = make_action(
        conn, run, action_type="submit_application", status="proposed", policy_decision="ask", payload={"kind": "relay"}
    )  # a payload of this name on another type changes nothing
    make_check(conn, action, "scope", True)
    make_approval(conn, action, status="approved")
    with refused(conn) as caught:
        move(conn, action, "scheduled")
    assert "to never" in str(caught.value)


# ---- row-level security as the real roles ------------------------------------------------------------------------


@pytest.fixture
def rls(conn, world):
    """The world plus a session with the employer's question, the Twin's released answer and the candidate's private
    reply, a second asked-only introduction of the same role, and a second organization."""
    session = twin_session(conn, world)
    question = employer_question(conn, session)
    action = executed_action(conn, world)
    answer = message(
        conn,
        session,
        author="twin",
        kind="answered",
        topic="skills",
        body="Answer.",
        cited=[world.candidate.record.fact_ids[0]],
        reply_to=question,
        action=action,
    )
    reply = message(conn, session, author="candidate", body="A private reply.", reply_to=question)
    pending = make_ask(conn, world.role, make_candidate(conn))
    other = twin_world(conn)
    twin_token = org_token(conn, world.org, world.org.owner, ["twin:ask"])
    ask_token = org_token(conn, world.org, world.org.owner, ["introductions:request"])
    return {
        "world": world,
        "session": session,
        "question": question,
        "answer": answer,
        "reply": reply,
        "pending": pending,
        "other": other,
        "twin_token": twin_token,
        "ask_token": ask_token,
    }


def n(conn, table: str) -> int:
    return one(conn, f"SELECT count(*) FROM {table}")  # noqa: S608 (a test's own table names)


def test_the_candidate_reads_their_session_and_every_message(conn, rls):
    act_as(conn, "app_api", rls["world"].candidate.id)
    assert n(conn, "twin_sessions") == 1
    assert n(conn, "twin_messages") == 3


def test_an_employer_member_reads_questions_and_released_answers_never_the_candidates_reply(conn, rls):
    act_as(conn, "app_api", rls["world"].member.user)
    assert n(conn, "twin_sessions") == 1
    assert n(conn, "twin_messages") == 2
    assert n(conn, "twin_messages WHERE author = 'candidate'") == 0


def test_another_organizations_member_and_a_stranger_read_nothing(conn, rls):
    for user in (
        rls["other"].member.user,
        rls["other"].candidate.id,
        rls["world"].org.owner and rls["other"].org.owner,
    ):
        act_as(conn, "app_api", user)
        assert (n(conn, "twin_sessions"), n(conn, "twin_messages")) == (0, 0)


def test_an_organization_token_with_twin_ask_reads_the_employers_view(conn, rls):
    token = rls["twin_token"]
    act_as(conn, "app_api", rls["world"].org.owner, token=token.id, scopes="twin:ask")
    assert (n(conn, "twin_sessions"), n(conn, "twin_messages")) == (1, 2)


def test_an_organization_token_without_twin_ask_sees_no_session(conn, rls):
    token = rls["ask_token"]
    act_as(conn, "app_api", rls["world"].org.owner, token=token.id, scopes="introductions:request")
    assert (n(conn, "twin_sessions"), n(conn, "twin_messages")) == (0, 0)


def test_an_organization_token_reads_accepted_introductions_their_shortlist_entries_and_roles(conn, rls):
    world = rls["world"]
    for scope, token in (("twin:ask", rls["twin_token"]), ("introductions:request", rls["ask_token"])):
        act_as(conn, "app_api", world.org.owner, token=token.id, scopes=scope)
        assert n(conn, "introductions") == 1  # the accepted one: the asked one is never shown
        assert n(conn, "introductions WHERE status = 'asked'") == 0
        assert n(conn, "shortlist_entries") == 1
        assert n(conn, "roles") == 1
        assert n(conn, "organizations") == 0 and n(conn, "org_members") == 0  # nothing else of the organization
    as_bootstrap(conn)
    withdraw(conn, world.accepted.consent)
    act_as(conn, "app_api", world.org.owner, token=rls["twin_token"].id, scopes="twin:ask")
    assert n(conn, "shortlist_entries") == 0  # the entry disappears with the consent


def test_an_organization_token_of_a_removed_member_reads_nothing(conn, rls):
    world = rls["world"]
    as_bootstrap(conn)
    conn.execute(
        text("DELETE FROM org_members WHERE organization_id = :o AND user_id = :u"),
        {"o": world.org.id, "u": world.org.owner},
    )
    act_as(conn, "app_api", world.org.owner, token=rls["twin_token"].id, scopes="twin:ask")
    assert (n(conn, "introductions"), n(conn, "roles"), n(conn, "twin_sessions")) == (0, 0, 0)


def test_a_candidate_token_with_matches_read_reads_shown_decisions_and_no_evidence_facts_or_campaigns(conn, rls):
    candidate = rls["world"].candidate
    desc = "We need Python and SQL."
    reqs = (("Python", "required"), ("SQL", "required"))
    job = match.make_job(conn, title="Data engineer", description=desc, requirements=reqs)
    run = match.make_run(conn, candidate.id)
    facts = candidate.record.fact_ids
    match.make_shown_decision(
        conn, run, candidate.id, job, [match.Verdict("met", (facts[0],)) for _ in job.requirement_ids]
    )
    match.make_decision(
        conn,
        run,
        candidate.id,
        match.make_job(conn, title="Analyst", description=desc, requirements=reqs),
        outcome="withheld",
    )
    token = candidate_token(conn, candidate.id, ["matches:read"])
    act_as(conn, "app_api", candidate.id, token=token.id, scopes="matches:read")
    assert n(conn, "match_decisions") == 1
    assert n(conn, "match_runs") == 1
    assert (n(conn, "match_evidence"), n(conn, "profile_facts"), n(conn, "campaigns")) == (0, 0, 0)
    assert (n(conn, "twin_sessions"), n(conn, "twin_messages"), n(conn, "introductions")) == (0, 0, 0)
    as_bootstrap(conn)
    own_evidence = one(conn, "SELECT count(*) FROM match_evidence WHERE user_id = :u", u=candidate.id)
    assert own_evidence >= 1
    both = candidate_token(conn, candidate.id, ["matches:read", "explanations:read"])
    act_as(conn, "app_api", candidate.id, token=both.id, scopes="matches:read,explanations:read")
    assert n(conn, "match_evidence") == own_evidence
    assert n(conn, "profile_facts") == 3


def test_admins_read_the_calls_of_their_organizations_tokens_and_members_do_not(conn, rls):
    world = rls["world"]
    conn.execute(
        text(
            """INSERT INTO audit_log (event_type, actor_type, actor_user_id, organization_id, agent_token_id, details)
               VALUES ('agentnet.agent_call', 'outside_agent', :u, :o, :t, CAST(:d AS jsonb))"""
        ),
        {"u": world.org.owner, "o": world.org.id, "t": rls["twin_token"].id, "d": json.dumps({"tool": "ask_twin"})},
    )
    act_as(conn, "app_api", world.org.owner)
    assert n(conn, "audit_log") == 1
    act_as(conn, "app_api", world.member.user)
    assert n(conn, "audit_log") == 0
    act_as(conn, "app_api", rls["other"].org.owner)
    assert n(conn, "audit_log") == 0
    act_as(conn, "app_api", world.org.owner, token=rls["twin_token"].id, scopes="twin:ask")
    assert n(conn, "audit_log") == 0  # a token never reads the log, even its own organization's


# ---- grants ----------------------------------------------------------------------------------------------------


def test_the_api_login_can_insert_messages_but_never_update_or_delete_them(conn, rls):
    world = rls["world"]
    act_as(conn, "app_api", world.member.user)
    conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
               VALUES (:s, :u, :o, 'employer', 'Another question?')"""
        ),
        {"s": rls["session"], "u": world.candidate.id, "o": world.org.id},
    )
    for sql in ("UPDATE twin_messages SET body = 'x'", "DELETE FROM twin_messages"):
        with refused(conn) as caught:
            conn.execute(text(sql))
        assert caught.value.orig.sqlstate == "42501", sql


def test_the_api_login_cannot_write_a_twin_answer_or_another_authors_message(conn, rls):
    world = rls["world"]
    act_as(conn, "app_api", world.member.user)
    with refused(conn) as caught:  # an employer cannot post as the Twin or the candidate
        conn.execute(
            text(
                """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind)
                   VALUES (:s, :u, :o, 'twin', 'Posted by the employer', 'skills', 'unknown')"""
            ),
            {"s": rls["session"], "u": world.candidate.id, "o": world.org.id},
        )
    assert caught.value.orig.sqlstate == "42501"


def test_the_worker_inserts_and_reads_messages_and_cannot_change_them(conn, rls):
    world = rls["world"]
    act_as(conn, "app_worker")
    conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic, answer_kind,
                                          in_reply_to)
               VALUES (:s, :u, :o, 'twin', 'Holding', 'skills', 'unknown', :q)"""
        ),
        {"s": rls["session"], "u": world.candidate.id, "o": world.org.id, "q": rls["question"]},
    )
    assert n(conn, "twin_messages") == 4
    with refused(conn) as caught:
        conn.execute(text("DELETE FROM twin_messages"))
    assert caught.value.orig.sqlstate == "42501"


def test_both_logins_read_and_write_scopes_and_the_api_cannot_move_a_session(conn, rls):
    world = rls["world"]
    for role, user in (("app_api", world.candidate.id), ("app_worker", None)):
        act_as(conn, role, user)
        conn.execute(
            text(
                """INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, 'skills', 'answer')
                   ON CONFLICT (user_id, topic) DO UPDATE SET permission = 'answer'"""
            ),
            {"u": world.candidate.id},
        )
        assert n(conn, "twin_scopes") == 1
    act_as(conn, "app_api", world.member.user)
    for sql in (
        "UPDATE twin_sessions SET status = 'closed', closed_at = now()",
        "DELETE FROM twin_sessions",
    ):
        with refused(conn) as caught:
            conn.execute(text(sql))
        assert caught.value.orig.sqlstate == "42501", sql
    act_as(conn, "app_worker")
    conn.execute(text("UPDATE twin_sessions SET status = 'closed', closed_at = now()"))
    assert n(conn, "twin_sessions WHERE status = 'closed'") == 1


def test_a_candidate_writes_only_their_own_scope_rows_and_replies(conn, rls):
    world = rls["world"]
    act_as(conn, "app_api", world.candidate.id)
    with refused(conn):  # a scope row for another person
        conn.execute(
            text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, 'skills', 'answer')"),
            {"u": rls["other"].candidate.id},
        )
    conn.execute(
        text(
            """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, in_reply_to)
               VALUES (:s, :u, :o, 'candidate', 'My own reply.', :q)"""
        ),
        {"s": rls["session"], "u": world.candidate.id, "o": world.org.id, "q": rls["question"]},
    )
    with refused(conn):  # the candidate may not speak as the employer
        conn.execute(
            text(
                """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
                   VALUES (:s, :u, :o, 'employer', 'Forged question')"""
            ),
            {"s": rls["session"], "u": world.candidate.id, "o": world.org.id},
        )


def test_an_outside_agents_context_cannot_write_the_candidates_scopes(conn, rls):
    world = rls["world"]
    token = candidate_token(conn, world.candidate.id, ["matches:read"])
    act_as(conn, "app_api", world.candidate.id, token=token.id, scopes="matches:read")
    with refused(conn):
        conn.execute(
            text("INSERT INTO twin_scopes (user_id, topic, permission) VALUES (:u, 'skills', 'answer')"),
            {"u": world.candidate.id},
        )
    assert n(conn, "twin_scopes") == 0
