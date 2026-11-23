"""Integration fix (PR #4): publishing and closing a role run in the API process, which holds only the API login. The
worker's login serves every person (its policies are USING (true)), so it stays in the worker container; on the Compose
stack POST /v1/roles/{id}/publish failed with OperationalError while publish asked for it.

Here the worker's and the match module's URLs point at nothing, so publish or close would fail the same way if either
opened another login. The writes app_api cannot make go through three functions that check membership again
(db/sql/p6_0601_publish_close.sql); a caller outside the organization, or an outside agent's token, is refused by each.
Everything is invented.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.hiring import roles
from app.platform import config, db
from tests.conftest import _empty_tables
from tests.hiring import world
from tests.hiring.factories import make_ask, make_candidate, make_open_role, make_org, make_role

UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


@pytest.fixture
def api_login_only(monkeypatch):
    current = config.get()
    changed = replace(current, database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make them again
    yield
    for made in db._engines.values():
        made.dispose()


def scalar(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def test_publishing_needs_only_the_api_login(clean, api_login_only):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_role(conn, org, confirmed=True, pay=(80_000, 120_000, "USD"))

    roles.publish(org.owner, role.id)

    assert set(db._engines) == {"api"}
    job_id = scalar(clean, "SELECT job_id FROM roles WHERE id = :id AND status = 'open'", id=role.id)
    assert scalar(clean, "SELECT status::text FROM jobs WHERE id = :id AND NOT is_listed", id=job_id) == "open"
    assert scalar(clean, "SELECT count(*) FROM job_requirements WHERE job_id = :id", id=job_id) == len(
        role.requirement_ids
    )


def test_closing_needs_only_the_api_login_and_still_withdraws_the_open_asks(clean, api_login_only):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_open_role(conn, org)
        ask = make_ask(conn, role, make_candidate(conn))

    roles.close(org.owner, role.id)

    assert set(db._engines) == {"api"}
    assert scalar(clean, "SELECT status::text FROM introductions WHERE id = :id", id=ask.id) == "withdrawn"
    assert scalar(clean, "SELECT status::text FROM jobs WHERE id = :id", id=role.job_id) == "closed"


@pytest.mark.parametrize("call", ["withdraw_role_asks(:role)", "direct_job_close(:job)"])
@pytest.mark.parametrize("who", ["outsider", "agent"])
def test_each_function_refuses_a_caller_outside_the_organization_and_an_agent_token(conn, call, who):
    org = make_org(conn)
    role = make_open_role(conn, org)
    make_ask(conn, role, make_candidate(conn))
    if who == "outsider":
        world.act_as(conn, "app_api", make_org(conn, "Brightwell Demo").owner)
    else:
        world.act_as(conn, "app_api", org.owner, token=uuid4())

    with pytest.raises(DBAPIError, match="not a member of the organization"):  # raised before any write
        conn.execute(text(f"SELECT {call}"), {"role": role.id, "job": role.job_id})


def test_the_worker_and_the_match_module_cannot_call_them(conn):
    for login in ("app_worker", "app_match"):
        allowed = conn.execute(
            text(
                "SELECT has_function_privilege(:login, 'direct_job_open(uuid, text, jsonb, text, text, text)', "
                "'EXECUTE') OR has_function_privilege(:login, 'direct_job_close(bigint)', 'EXECUTE') "
                "OR has_function_privilege(:login, 'withdraw_role_asks(uuid)', 'EXECUTE')"
            ),
            {"login": login},
        ).scalar_one()
        assert allowed is False, login
