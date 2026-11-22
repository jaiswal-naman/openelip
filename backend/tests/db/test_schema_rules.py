"""The rules the Phase 1 schema enforces itself: roles, append-only and frozen rows, row-level security."""

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError


def _user(conn: Connection, email: str):
    return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


def _session(conn: Connection, user_id, token: bytes) -> None:
    conn.execute(
        text("INSERT INTO sessions (user_id, token_sha256, expires_at) VALUES (:u, :t, now() + interval '1 day')"),
        {"u": user_id, "t": token},
    )


def _act_as(conn: Connection, role: str, user_id=None) -> None:
    """Drop the bootstrap superuser's privileges for the rest of the test transaction, like a request does."""
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


def _refused(conn: Connection):
    """pytest.raises around a statement that must fail, inside a savepoint so the test transaction survives."""

    class Refused:
        def __enter__(self):
            self._savepoint = conn.begin_nested()
            self._raises = pytest.raises(DBAPIError)
            return self._raises.__enter__()

        def __exit__(self, *exc):
            handled = self._raises.__exit__(*exc)
            self._savepoint.rollback()
            return handled

    return Refused()


def test_roles_are_as_the_architecture_describes(conn):
    roles = {r.rolname: r for r in conn.execute(text("SELECT * FROM pg_roles WHERE rolname LIKE 'app\\_%'"))}
    assert not roles["app_owner"].rolcanlogin
    for name in ("app_api", "app_worker"):
        assert roles[name].rolcanlogin and not roles[name].rolbypassrls and not roles[name].rolsuper


def test_app_owner_owns_every_table(conn):
    owners = (
        conn.execute(
            text(
                "SELECT DISTINCT tableowner FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        )
        .scalars()
        .all()
    )
    assert owners == ["app_owner"]


def test_the_cost_ledger_is_append_only(conn):
    model = conn.execute(
        text(
            "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
            "VALUES ('local', 'test-model', 'chat', true, 'test') RETURNING id"
        )
    ).scalar_one()
    conn.execute(
        text(
            "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, latency_ms) "
            "VALUES ('ping', 'v1', :m, 'ok', 5)"
        ),
        {"m": model},
    )
    with _refused(conn):
        conn.execute(text("UPDATE llm_calls SET cost_microusd = 1"))
    with _refused(conn):
        conn.execute(text("DELETE FROM llm_calls"))


def test_an_eval_run_is_frozen_once_it_finishes(conn):
    run = conn.execute(
        text("INSERT INTO eval_runs (suite, git_sha) VALUES ('harness_smoke', 'abc') RETURNING id")
    ).scalar_one()
    finish = text("UPDATE eval_runs SET status = 'passed', metrics = '{}'::jsonb, finished_at = now() WHERE id = :i")
    conn.execute(finish, {"i": run})  # a running run may finish once
    with _refused(conn):
        conn.execute(text("UPDATE eval_runs SET is_gate_passed = true WHERE id = :i"), {"i": run})
    with _refused(conn):
        conn.execute(text("DELETE FROM eval_runs WHERE id = :i"), {"i": run})
    conn.execute(text("INSERT INTO eval_items (eval_run_id, item_key, is_passed) VALUES (:i, 'a', true)"), {"i": run})
    with _refused(conn):
        conn.execute(text("UPDATE eval_items SET is_passed = false"))


def test_a_signed_in_person_sees_only_their_own_rows(conn):
    a, b = _user(conn, "a@example.test"), _user(conn, "b@example.test")
    _session(conn, a, b"a" * 32)
    _session(conn, b, b"b" * 32)

    _act_as(conn, "app_api", a)
    assert conn.execute(text("SELECT id FROM users")).scalars().all() == [a]
    assert conn.execute(text("SELECT user_id FROM sessions")).scalars().all() == [a]
    with _refused(conn):  # writing a row for someone else is refused as well
        _session(conn, b, b"c" * 32)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # no signed-in person: nothing is visible
    assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
    assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 0


def test_the_ledger_is_scoped_to_the_person_it_names(conn):
    a, b = _user(conn, "a@example.test"), _user(conn, "b@example.test")
    model = conn.execute(
        text(
            "INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note) "
            "VALUES ('local', 'test-model', 'chat', true, 'test') RETURNING id"
        )
    ).scalar_one()
    for user in (a, b, b):
        conn.execute(
            text(
                "INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, latency_ms) "
                "VALUES ('ping', 'v1', :m, 'ok', :u, 5)"
            ),
            {"m": model, "u": user},
        )
    _act_as(conn, "app_api", a)
    assert conn.execute(text("SELECT count(*) FROM llm_calls")).scalar_one() == 1


def test_the_worker_serves_everyone(conn):
    a, b = _user(conn, "a@example.test"), _user(conn, "b@example.test")
    _session(conn, a, b"a" * 32)
    _session(conn, b, b"b" * 32)
    _act_as(conn, "app_worker")
    assert conn.execute(text("SELECT count(*) FROM sessions")).scalar_one() == 2


def test_the_sign_in_entry_points_work_before_a_user_id_exists(conn):
    """Row-level security hides sessions and users from a caller with no user id; these functions are the way in."""
    existing = _user(conn, "Known@Example.test")
    _session(conn, existing, b"k" * 32)
    _act_as(conn, "app_api")  # the API has no user id yet: that is why these are SECURITY DEFINER

    assert conn.execute(text("SELECT resolve_session(:t)"), {"t": b"k" * 32}).scalar_one() == existing
    assert conn.execute(text("SELECT resolve_session(:t)"), {"t": b"z" * 32}).scalar_one() is None
    assert conn.execute(text("SELECT user_for_verified_email('known@example.TEST')")).scalar_one() == existing
    created = conn.execute(text("SELECT user_for_verified_email('new@example.test')")).scalar_one()
    assert created != existing
    assert conn.execute(text("SELECT user_for_verified_email('NEW@example.test')")).scalar_one() == created
    assert conn.execute(text("SELECT count(*) FROM users")).scalar_one() == 0  # and the table is still closed
