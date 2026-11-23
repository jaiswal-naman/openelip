"""Revision p5_0501_chat_tailor_apply, chat (plan 05-01): what a message row may say, how a reply ends, and the link to
the cost ledger.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Rows are invented
(tests/match/factories.py). Who may read chat rows and what erasure leaves of them is in test_tailor_apply_schema.py,
which checks all seven tables of the revision together.
"""

from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from tests.match.factories import make_user

FINISH_REASONS = ("complete", "length", "stopped", "interrupted")


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def one(conn: Connection, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def make_session(conn: Connection, user: UUID, kind: str = "open") -> UUID:
    return one(conn, "INSERT INTO chat_sessions (user_id, kind) VALUES (:u, :k) RETURNING id", u=user, k=kind)


def add_message(
    conn: Connection,
    session: UUID,
    user: UUID,
    role: str,
    *,
    content: str | None = None,
    tool_name: str | None = None,
    finish_reason: str | None = None,
    llm_call_id: int | None = None,
) -> UUID:
    return one(
        conn,
        """INSERT INTO chat_messages (session_id, user_id, role, content, tool_name, finish_reason, llm_call_id)
           VALUES (:s, :u, :role, :content, :tool, :finish, :call) RETURNING id""",
        s=session,
        u=user,
        role=role,
        content=content,
        tool=tool_name,
        finish=finish_reason,
        call=llm_call_id,
    )


def test_a_session_is_an_onboarding_or_an_open_one(conn):
    ada = make_user(conn)
    make_session(conn, ada, "onboarding")
    make_session(conn, ada, "open")
    with refused(conn) as caught:
        make_session(conn, ada, "private")
    assert caught.value.orig.sqlstate == "22P02"


def test_each_kind_of_message_row_is_stored_as_the_agent_writes_it(conn):
    ada = make_user(conn)
    session = make_session(conn, ada)
    add_message(conn, session, ada, "user", content="Why was the first job ruled out?")
    add_message(conn, session, ada, "tool", tool_name="get_decision")
    add_message(conn, session, ada, "assistant", content="It needs onsite work. [[d1]]", finish_reason="complete")
    rows = conn.execute(
        text("SELECT role, finish_reason, refs FROM chat_messages WHERE session_id = :s ORDER BY created_at, role"),
        {"s": session},
    ).all()
    assert {row.role for row in rows} == {"user", "tool", "assistant"}
    assert all(row.refs == {} for row in rows)  # refs default to an empty object


@pytest.mark.parametrize(
    "row",
    [
        {"role": "tool", "content": None, "tool_name": None},  # a tool row says which tool ran
        {"role": "tool", "content": "text", "tool_name": "get_record"},  # and carries no text of its own
        {"role": "user", "content": "Hi", "tool_name": "get_record"},  # only a tool row has a tool name
        {
            "role": "assistant",
            "content": None,
            "tool_name": None,
        },  # a reply that is not a tool row has text (maybe empty)
    ],
    ids=["tool-without-name", "tool-with-text", "user-with-tool-name", "assistant-without-text"],
)
def test_a_message_row_that_breaks_the_shape_is_refused(conn, row):
    ada = make_user(conn)
    session = make_session(conn, ada)
    with refused(conn) as caught:
        add_message(conn, session, ada, row["role"], content=row["content"], tool_name=row["tool_name"])
    assert caught.value.orig.sqlstate == "23514"


def test_a_reply_that_is_still_running_has_no_finish_reason_yet(conn):
    ada = make_user(conn)
    session = make_session(conn, ada)
    reply = add_message(conn, session, ada, "assistant", content="")  # accepted at once, filled in as it runs
    assert one(conn, "SELECT finish_reason FROM chat_messages WHERE id = :m", m=reply) is None
    conn.execute(
        text("UPDATE chat_messages SET content = 'Done.', finish_reason = 'complete' WHERE id = :m"), {"m": reply}
    )
    assert one(conn, "SELECT finish_reason FROM chat_messages WHERE id = :m", m=reply) == "complete"


@pytest.mark.parametrize("reason", FINISH_REASONS)
def test_every_way_a_reply_can_end_is_accepted(conn, reason):
    ada = make_user(conn)
    session = make_session(conn, ada)
    reply = add_message(conn, session, ada, "assistant", content="Text.", finish_reason=reason)
    assert one(conn, "SELECT finish_reason FROM chat_messages WHERE id = :m", m=reply) == reason


@pytest.mark.parametrize("reason", ["done", "", "COMPLETE", "error"])
def test_any_other_finish_reason_is_refused(conn, reason):
    ada = make_user(conn)
    session = make_session(conn, ada)
    with refused(conn) as caught:
        add_message(conn, session, ada, "assistant", content="Text.", finish_reason=reason)
    assert caught.value.orig.sqlstate == "23514"


def test_a_sessions_messages_go_with_it(conn):
    ada = make_user(conn)
    session = make_session(conn, ada)
    add_message(conn, session, ada, "user", content="Hi")
    add_message(conn, session, ada, "assistant", content="Hello.", finish_reason="complete")
    conn.execute(text("DELETE FROM chat_sessions WHERE id = :s"), {"s": session})
    assert one(conn, "SELECT count(*) FROM chat_messages WHERE session_id = :s", s=session) == 0


def make_model_call(conn: Connection, user: UUID) -> int:
    """A cost-ledger row, with the model row it needs."""
    model = one(
        conn,
        """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
           VALUES ('local', :m, 'chat', true, 'test') RETURNING id""",
        m=f"model-{uuid4().hex}",
    )
    return one(
        conn,
        """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, latency_ms)
           VALUES ('chat.turn', 'v1', :m, 'ok', :u, 5) RETURNING id""",
        m=model,
        u=user,
    )


def test_a_message_names_the_model_call_that_wrote_it_and_survives_its_deletion(conn):
    ada = make_user(conn)
    session = make_session(conn, ada)
    call = make_model_call(conn, ada)
    reply = add_message(conn, session, ada, "assistant", content="Text.", finish_reason="complete", llm_call_id=call)
    assert one(conn, "SELECT llm_call_id FROM chat_messages WHERE id = :m", m=reply) == call
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))  # the ledger is append-only except for this
    conn.execute(text("DELETE FROM llm_calls WHERE id = :c"), {"c": call})
    assert one(conn, "SELECT count(*) FROM chat_messages WHERE id = :m", m=reply) == 1
    assert one(conn, "SELECT llm_call_id FROM chat_messages WHERE id = :m", m=reply) is None


def test_a_message_cannot_name_a_model_call_that_does_not_exist(conn):
    ada = make_user(conn)
    session = make_session(conn, ada)
    with refused(conn) as caught:
        add_message(conn, session, ada, "assistant", content="Text.", llm_call_id=987654321)
    assert caught.value.orig.sqlstate == "23503"
