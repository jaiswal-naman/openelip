"""erase_user and purge_expired (TRU-02, DATA_MODEL 7.2) and the rules of the data_requests table that carries them.

Rows are made with plain SQL and the functions are called as the worker role, the only role allowed to run them.
"""

from contextlib import contextmanager
from datetime import timedelta
from typing import NamedTuple
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.trust import api

# Tables with a user_id column that erase_user deletes by cascade. users (id) and audit_log (subject_user_id) are
# checked beside them.
CASCADE_TABLES = ("sessions", "consents", "outbox", "queue_jobs", "idempotency_keys")
NO_REQUEST = "00000000-0000-0000-0000-000000000000"


class Person(NamedTuple):
    user: UUID
    email: str


def act_as(conn, role: str, user_id=None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


@contextmanager
def refused(conn):
    """One statement that must fail, inside a savepoint so the test's transaction survives it."""
    with pytest.raises(DBAPIError):
        with conn.begin_nested():
            yield


def run(conn, sql: str, **params) -> None:
    conn.execute(text(sql), params)


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def count(conn, source: str, **params) -> int:
    return one(conn, f"SELECT count(*) FROM {source}", **params)


def seed_person(conn, name: str) -> Person:
    """A person with a row in every table that holds something about them at this revision."""
    unique = uuid4().hex
    email = f"{name}@example.test"
    user = one(conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=email)
    model = one(
        conn,
        """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
           VALUES ('local', :m, 'chat', true, 'test') RETURNING id""",
        m=f"model-{unique}",
    )
    run(
        conn,
        """INSERT INTO sessions (user_id, token_sha256, expires_at)
           VALUES (:u, :t, now() + interval '1 day')""",
        u=user,
        t=uuid4().bytes * 2,
    )
    run(
        conn,
        """INSERT INTO login_codes (email, code_hmac, expires_at)
           VALUES (:e, :t, now() + interval '10 minutes')""",
        e=email.upper(),  # another spelling: erase_user matches on lower(email)
        t=b"h" * 32,
    )
    run(
        conn,
        "INSERT INTO outbox (dedupe_key, user_id, template) VALUES (:k, :u, 'brief_v1')",
        k=f"brief:{unique}",
        u=user,
    )
    run(
        conn,
        "INSERT INTO queue_jobs (queue, kind, unique_key, user_id) VALUES ('default', 'x.run', :k, :u)",
        k=f"x.run:{unique}",
        u=user,
    )
    run(
        conn,
        """INSERT INTO idempotency_keys (user_id, key, method, path, request_sha256)
           VALUES (:u, 'idempotency-key-1', 'POST', '/v1/x', :t)""",
        u=user,
        t=b"h" * 32,
    )
    run(
        conn,
        """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, latency_ms)
           VALUES ('ping', 'v1', :m, 'ok', :u, 5)""",
        m=model,
        u=user,
    )
    api.grant_consent(conn, user, "terms", "terms-v1")  # a consent row and an audit row about them
    return Person(user, email)


def open_request(conn, user_id, *, kind: str = "deletion"):
    """A request row about the person."""
    return one(
        conn,
        """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
           VALUES (:u, :u, :k, now() + interval '30 days') RETURNING id""",
        u=user_id,
        k=kind,
    )


def erase(conn, user_id, request_id):
    """erase_user as the worker role. The role is set inside the call, so a refusal inside `refused` undoes it too."""
    act_as(conn, "app_worker")
    result = one(conn, "SELECT erase_user(:u, :r)", u=user_id, r=request_id)
    conn.execute(text("RESET ROLE"))
    return result


def test_erase_user_removes_the_person_and_keeps_only_what_must_outlive_them(conn):
    ada, bob = seed_person(conn, "ada"), seed_person(conn, "bob")
    bobs_audit_rows = count(conn, "audit_log WHERE subject_user_id = :u", u=bob.user)
    # an event about Bob that Ada caused: the row stays, without the reference to her
    api.audit_log(conn, "model.provider_changed", "operator", bob.user, actor_user_id=ada.user)

    result = erase(conn, ada.user, open_request(conn, ada.user))

    assert result == {"postgres": "deleted"}
    assert count(conn, "users WHERE id = :u", u=ada.user) == 0
    assert count(conn, "audit_log WHERE subject_user_id = :u OR actor_user_id = :u", u=ada.user) == 0
    assert count(conn, "login_codes WHERE lower(email) = :e", e=ada.email) == 0
    for table in CASCADE_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=ada.user) == 0, table
    # the cost ledger keeps its row, with no reference to the person
    assert count(conn, "llm_calls WHERE user_id IS NULL") == 1
    # the event Ada caused about Bob stays, with the actor cleared
    row = conn.execute(text("SELECT * FROM audit_log WHERE event_type = 'model.provider_changed'")).one()
    assert (row.actor_user_id, row.subject_user_id) == (None, bob.user)

    # Bob is untouched
    assert count(conn, "users WHERE id = :u", u=bob.user) == 1
    assert count(conn, "login_codes WHERE lower(email) = :e", e=bob.email) == 1
    assert count(conn, "llm_calls WHERE user_id = :u", u=bob.user) == 1
    for table in CASCADE_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=bob.user) == 1, table
    assert count(conn, "audit_log WHERE subject_user_id = :u", u=bob.user) == bobs_audit_rows + 1


def test_erase_user_keeps_the_request_row_with_the_bare_id_and_says_what_it_did(conn):
    ada = seed_person(conn, "ada")
    request = open_request(conn, ada.user)
    erase(conn, ada.user, request)

    row = conn.execute(text("SELECT * FROM data_requests WHERE id = :r"), {"r": request}).one()
    assert (row.user_id, row.subject_user_id, row.status) == (None, ada.user, "completed")
    assert row.completed_at is not None
    assert row.store_results == {"postgres": "deleted", "login_codes": 1}

    event = conn.execute(text("SELECT * FROM audit_log WHERE event_type = 'user.erased'")).one()
    assert (event.actor_type, event.subject_user_id, event.entity_table) == ("system", None, "data_requests")
    assert event.entity_id == str(request)


def test_no_uuid_column_names_the_person_afterwards_except_the_kept_request(conn):
    """A small canary: the id is looked for in every uuid column of every table (the full scan is plan 03-06)."""
    ada = seed_person(conn, "ada")
    erase(conn, ada.user, open_request(conn, ada.user))
    uuid_columns = conn.execute(
        text(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND udt_name = 'uuid'"""
        )
    ).all()
    holding = [
        (table, column) for table, column in uuid_columns if count(conn, f'"{table}" WHERE "{column}" = :u', u=ada.user)
    ]
    assert holding == [("data_requests", "subject_user_id")]


@pytest.mark.parametrize("what", ["no request", "an export request", "someone else's request"])
def test_erase_user_needs_an_open_deletion_request_for_that_person(conn, what):
    ada, bob = seed_person(conn, "ada"), seed_person(conn, "bob")
    request = {
        "no request": lambda: NO_REQUEST,
        "an export request": lambda: open_request(conn, ada.user, kind="export"),
        "someone else's request": lambda: open_request(conn, bob.user),
    }[what]()
    with refused(conn):
        erase(conn, ada.user, request)
    assert count(conn, "users WHERE id = :u", u=ada.user) == 1
    assert count(conn, "sessions WHERE user_id = :u", u=ada.user) == 1


def test_a_restored_person_is_erased_again_through_a_new_request_never_the_finished_one(conn):
    """The kept subject_user_id is why a database restored from before the erasure can be erased again."""
    ada = seed_person(conn, "ada")
    first = open_request(conn, ada.user)
    erase(conn, ada.user, first)
    run(conn, "INSERT INTO users (id, email) VALUES (:u, :e)", u=ada.user, e=ada.email)  # the restore brings them back

    with refused(conn):
        erase(conn, ada.user, first)  # a finished request cannot be used again
    assert count(conn, "users WHERE id = :u", u=ada.user) == 1

    erase(conn, ada.user, open_request(conn, ada.user))  # the kept id is all that is needed to open a new one
    assert count(conn, "users WHERE id = :u", u=ada.user) == 0


def test_only_the_worker_role_can_run_erase_user_or_purge_expired(conn):
    ada = seed_person(conn, "ada")
    request = open_request(conn, ada.user)
    act_as(conn, "app_api", ada.user)
    with refused(conn):
        run(conn, "SELECT erase_user(:u, :r)", u=ada.user, r=request)
    with refused(conn):
        run(conn, "SELECT purge_expired()")
    conn.execute(text("RESET ROLE"))
    assert count(conn, "users WHERE id = :u", u=ada.user) == 1


def test_purge_expired_clears_only_archives_past_their_expiry(conn):
    ada = seed_person(conn, "ada")
    insert = """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at, export_archive, export_expires_at)
                VALUES (:u, :u, 'export', now(), :a, now() + :offset) RETURNING id"""
    stale = one(conn, insert, u=ada.user, a=b"old", offset=timedelta(days=-1))
    fresh = one(conn, insert, u=ada.user, a=b"new", offset=timedelta(days=1))
    act_as(conn, "app_worker")
    assert one(conn, "SELECT purge_expired()") == {"export_links": 1}
    conn.execute(text("RESET ROLE"))
    archives = dict(conn.execute(text("SELECT id, export_archive FROM data_requests")).all())
    assert archives[stale] is None and bytes(archives[fresh]) == b"new"


def test_one_open_deletion_request_per_person(conn):
    ada = seed_person(conn, "ada")
    open_request(conn, ada.user)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            open_request(conn, ada.user)
    open_request(conn, ada.user, kind="export")  # other kinds are not limited


def test_a_request_is_completed_only_with_a_completion_time(conn):
    ada = seed_person(conn, "ada")
    with refused(conn):
        run(
            conn,
            """INSERT INTO data_requests (user_id, subject_user_id, kind, status, due_at)
               VALUES (:u, :u, 'export', 'completed', now())""",
            u=ada.user,
        )


def test_the_api_role_cannot_open_a_request_that_erases_someone_else(conn):
    """erase_user trusts a request's subject, so the policy checks it as well as user_id (T-3-03)."""
    ada, bob = seed_person(conn, "ada"), seed_person(conn, "bob")
    forged = """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
                VALUES (:u, :s, 'deletion', now() + interval '30 days')"""
    act_as(conn, "app_api", ada.user)
    with refused(conn):
        run(conn, forged, u=ada.user, s=bob.user)  # mine, but about Bob
    with refused(conn):
        run(conn, forged, u=bob.user, s=bob.user)  # Bob's own request, written by Ada
    mine = open_request(conn, ada.user)  # an honest request is fine
    with refused(conn):
        run(conn, "UPDATE data_requests SET subject_user_id = :s WHERE id = :r", s=bob.user, r=mine)
    conn.execute(text("RESET ROLE"))
    assert count(conn, "data_requests WHERE subject_user_id = :u", u=bob.user) == 0


def test_a_person_sees_only_their_own_requests(conn):
    ada, bob = seed_person(conn, "ada"), seed_person(conn, "bob")
    open_request(conn, ada.user)
    open_request(conn, bob.user)
    act_as(conn, "app_api", ada.user)
    assert count(conn, "data_requests") == 1
    with refused(conn):
        open_request(conn, bob.user, kind="export")
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_worker")
    assert count(conn, "data_requests") == 2
