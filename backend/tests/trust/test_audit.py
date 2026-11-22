"""The audit writer: append-only rows that hold ids, counts and codes and never text (TRU-03, PLT-07, T-3-04)."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.trust import api


def make_user(conn, email: str = "ada@example.test"):
    return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


def act_as(conn, role: str, user_id=None) -> None:
    """Drop the bootstrap superuser's privileges for the rest of the test transaction, like a request does."""
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


@contextmanager
def refused(conn):
    """One statement that must fail, inside a savepoint so the test's transaction survives it."""
    with pytest.raises(DBAPIError):
        with conn.begin_nested():
            yield


def rows(conn, sql: str = "SELECT * FROM audit_log ORDER BY id"):
    return conn.execute(text(sql)).all()


@pytest.mark.parametrize(
    "event_type",
    [
        "consent",
        "Consent.granted",
        "consent.granted.now",
        "consent-granted.x",
        "consent.granted1",
        "",
        "consent.",
        ".x",
    ],
)
def test_an_event_type_must_be_module_dot_name(conn, event_type):
    with pytest.raises(ValueError):
        api.audit_log(conn, event_type, "system", None)
    assert rows(conn) == []


def test_the_table_refuses_a_bad_event_type_too(conn):
    """The writer checks first so a caller's transaction is not spoiled; the CHECK is the last line of defence."""
    with refused(conn):
        conn.execute(text("INSERT INTO audit_log (event_type, actor_type) VALUES ('nodot', 'system')"))


def test_a_detail_may_be_200_characters_and_no_more(conn):
    api.audit_log(conn, "match.run_completed", "system", None, {"code": "x" * 200})
    with pytest.raises(ValueError):
        api.audit_log(conn, "match.run_completed", "system", None, {"code": "x" * 201})
    assert len(rows(conn)) == 1


@pytest.mark.parametrize("value", [["a"], {"a": 1}, 1.5, b"bytes", object()])
def test_a_detail_is_a_code_an_id_a_count_or_a_flag_never_a_structure(conn, value):
    with pytest.raises(ValueError):
        api.audit_log(conn, "match.run_completed", "system", None, {"what": value})
    assert rows(conn) == []


def test_ids_counts_flags_and_codes_are_stored_as_given(conn):
    user, thing = make_user(conn), uuid4()
    api.audit_log(conn, "match.run_completed", "system", user, {"run_id": thing, "shown": 3, "ok": True, "gone": None})
    (row,) = rows(conn)
    assert row.details == {"run_id": str(thing), "shown": 3, "ok": True, "gone": None}
    assert (row.event_type, row.subject_user_id) == ("match.run_completed", user)


def test_nothing_given_stores_an_empty_object(conn):
    api.audit_log(conn, "user.signed_in", "system", None)
    assert rows(conn)[0].details == {}


def test_a_candidate_acts_on_their_own_record(conn):
    user = make_user(conn)
    api.audit_log(
        conn, "consent.granted", "candidate", user, {"purpose": "terms"}, entity_table="consents", entity_id="7"
    )
    (row,) = rows(conn)
    assert (row.actor_type, row.actor_user_id, row.subject_user_id) == ("candidate", user, user)
    assert (row.entity_table, row.entity_id) == ("consents", "7")


def test_a_system_event_has_no_actor_and_an_operator_names_theirs(conn):
    user, operator = make_user(conn), make_user(conn, "operator@example.test")
    api.audit_log(conn, "model.provider_changed", "system", user)
    api.audit_log(conn, "model.provider_changed", "operator", user, actor_user_id=operator)
    assert [(r.actor_type, r.actor_user_id) for r in rows(conn)] == [("system", None), ("operator", operator)]


def test_an_unknown_actor_type_is_refused_before_the_database_sees_it(conn):
    with pytest.raises(ValueError):
        api.audit_log(conn, "consent.granted", "robot", None)
    assert rows(conn) == []


def test_the_log_cannot_be_rewritten_by_anyone(conn):
    api.audit_log(conn, "user.signed_in", "system", None)
    with refused(conn):
        conn.execute(text("UPDATE audit_log SET event_type = 'user.other'"))
    with refused(conn):
        conn.execute(text("DELETE FROM audit_log"))
    assert len(rows(conn)) == 1


def test_a_person_reads_only_the_rows_about_them_and_the_worker_reads_all(conn):
    a, b = make_user(conn, "a@example.test"), make_user(conn, "b@example.test")
    api.audit_log(conn, "consent.granted", "candidate", a)
    api.audit_log(conn, "consent.granted", "candidate", b)
    api.audit_log(conn, "user.signed_in", "system", None)

    act_as(conn, "app_api", a)
    assert [r.subject_user_id for r in rows(conn)] == [a]
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_worker")
    assert len(rows(conn)) == 3


def test_the_api_role_writes_rows_about_the_person_it_acts_for_and_no_one_else(conn):
    a, b = make_user(conn, "a@example.test"), make_user(conn, "b@example.test")
    act_as(conn, "app_api", a)
    api.audit_log(conn, "consent.granted", "candidate", a)  # the policy needs actor_user_id = the signed-in person
    with refused(conn):
        api.audit_log(conn, "consent.granted", "candidate", b, actor_user_id=b)
    with refused(conn):
        api.audit_log(conn, "model.provider_changed", "system", a)  # a system row has no actor: the worker writes it
    assert len(rows(conn)) == 1
