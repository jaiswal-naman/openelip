"""Revision p4_0401_match_brief, the brief half: one brief a day per channel, what it lists, who reads it (plan 04-01).

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Rows are invented
(tests/match/factories.py).
"""

import json
from contextlib import contextmanager
from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.brief import privacy as brief_privacy
from app.trust import privacy
from tests.match.factories import LOCAL_DATE, make_decision, make_job, make_run, make_user


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def count(conn: Connection, sql: str, **params) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {sql}"), params).scalar_one()


def make_brief(
    conn: Connection, user: UUID, *, channel: str = "email", day=LOCAL_DATE, run: UUID | None = None
) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO briefs (user_id, local_date, channel, match_run_id) VALUES (:u, :d, :c, :r)
               RETURNING id"""
        ),
        {"u": user, "d": day, "c": channel, "r": run},
    ).scalar_one()


def add_item(conn: Connection, brief: UUID, user: UUID, position: int, kind: str, **ids) -> None:
    columns = {"match_decision_id": None, "approval_id": None, "agent_action_id": None, "introduction_id": None} | ids
    conn.execute(
        text(
            """INSERT INTO brief_items (brief_id, position, user_id, kind, match_decision_id, approval_id,
                                        agent_action_id, introduction_id)
               VALUES (:b, :p, :u, :k, :match_decision_id, :approval_id, :agent_action_id, :introduction_id)"""
        ),
        {"b": brief, "p": position, "u": user, "k": kind, **columns},
    )


# ---- BRF-01: one brief a day per channel -----------------------------------------------------------------------


def test_one_brief_per_person_date_and_channel(conn):
    ada, bob = make_user(conn), make_user(conn)
    make_brief(conn, ada)
    with refused(conn) as caught:  # a second email brief for the same day: the double-send guard
        make_brief(conn, ada)
    assert caught.value.orig.sqlstate == "23505" and caught.value.orig.diag.constraint_name == "briefs_one_per_day"
    make_brief(conn, ada, channel="in_app")  # the in-app brief is its own
    make_brief(conn, ada, day=LOCAL_DATE + timedelta(days=1))
    make_brief(conn, bob)
    assert count(conn, "briefs WHERE user_id = ANY(:u)", u=[ada, bob]) == 4


def test_a_new_brief_starts_composed_and_a_deleted_run_leaves_it_standing(conn):
    ada = make_user(conn)
    run = make_run(conn, ada)
    brief = make_brief(conn, ada, run=run)
    assert conn.execute(text("SELECT status FROM briefs WHERE id = :b"), {"b": brief}).scalar_one() == "composed"
    conn.execute(text("DELETE FROM match_runs WHERE id = :r"), {"r": run})  # a run with no decisions can go
    assert conn.execute(text("SELECT match_run_id FROM briefs WHERE id = :b"), {"b": brief}).scalar_one() is None


# ---- what a brief lists ----------------------------------------------------------------------------------------


def test_an_item_names_exactly_the_one_thing_its_kind_says(conn):
    ada = make_user(conn)
    job = make_job(conn, title="Role", description="Skill.", requirements=[("Skill.", "required")])
    decision = make_decision(conn, make_run(conn, ada), ada, job)
    brief = make_brief(conn, ada)
    add_item(conn, brief, ada, 1, "match", match_decision_id=decision)
    # the other three kinds point at tables of later phases: the ids are plain columns until those tables exist
    add_item(conn, brief, ada, 2, "approval", approval_id=ada)
    add_item(conn, brief, ada, 3, "activity", agent_action_id=7)
    add_item(conn, brief, ada, 4, "introduction", introduction_id=ada)
    assert count(conn, "brief_items WHERE brief_id = :b", b=brief) == 4


@pytest.mark.parametrize(
    "item",
    [
        {"position": 1, "kind": "match"},  # a match with no decision
        {"position": 1, "kind": "approval"},  # an approval with no approval id
        {"position": 1, "kind": "activity", "approval_id": "a"},  # the wrong id for the kind
        {"position": 1, "kind": "approval", "approval_id": "a", "agent_action_id": 7},  # two ids at once
        {"position": 0, "kind": "activity", "agent_action_id": 7},  # positions start at 1
    ],
    ids=["match-without-decision", "approval-without-id", "wrong-id-for-kind", "two-ids", "position-zero"],
)
def test_an_item_that_does_not_say_what_it_lists_is_refused(conn, item):
    ada = make_user(conn)
    brief = make_brief(conn, ada)
    ids = {key: (ada if value == "a" else value) for key, value in item.items() if key.endswith("_id")}
    with refused(conn) as caught:
        add_item(conn, brief, ada, item["position"], item["kind"], **ids)
    assert caught.value.orig.sqlstate == "23514"


def test_a_position_in_a_brief_is_used_once_and_items_go_with_their_brief(conn):
    ada = make_user(conn)
    brief = make_brief(conn, ada)
    add_item(conn, brief, ada, 1, "activity", agent_action_id=1)
    with refused(conn) as caught:
        add_item(conn, brief, ada, 1, "activity", agent_action_id=2)
    assert caught.value.orig.sqlstate == "23505"
    conn.execute(text("DELETE FROM briefs WHERE id = :b"), {"b": brief})
    assert count(conn, "brief_items WHERE brief_id = :b", b=brief) == 0


# ---- who may read it -------------------------------------------------------------------------------------------


def act_as(conn: Connection, role: str, user_id: UUID | None = None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


def test_a_signed_in_person_reads_their_own_briefs_and_the_worker_reads_all(conn):
    ada, bob = make_user(conn), make_user(conn)
    for user in (ada, bob):
        add_item(conn, make_brief(conn, user), user, 1, "activity", agent_action_id=1)
    act_as(conn, "app_api", ada)
    assert count(conn, "briefs") == 1 and count(conn, "brief_items") == 1
    assert count(conn, "briefs WHERE user_id = :u", u=bob) == 0
    with refused(conn) as caught:  # a brief in someone else's name
        make_brief(conn, bob, channel="in_app")
    assert caught.value.orig.sqlstate == "42501"
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_worker")
    assert count(conn, "briefs WHERE user_id = ANY(:u)", u=[ada, bob]) == 2  # the worker composes for everyone
    make_brief(conn, ada, channel="in_app")


# ---- the export and the registry -------------------------------------------------------------------------------


def test_the_brief_exporter_returns_the_persons_briefs_and_items_and_nobody_elses(conn):
    ada, bob = make_user(conn), make_user(conn)
    briefs = {user: make_brief(conn, user) for user in (ada, bob)}
    for user, brief in briefs.items():
        add_item(conn, brief, user, 1, "activity", agent_action_id=1)

    export = brief_privacy.export_brief(conn, ada)

    assert privacy.exporters()["brief"] is brief_privacy.export_brief  # importing the module registered it
    assert privacy.registry()["brief"] == {"briefs": "cascade", "brief_items": "cascade"}
    assert set(export.data) == {"briefs", "brief_items"} and export.files == {}
    assert [row["id"] for row in export.data["briefs"]] == [str(briefs[ada])]
    assert [row["brief_id"] for row in export.data["brief_items"]] == [str(briefs[ada])]
    assert str(bob) not in json.dumps(export.data)  # JSON-ready as it stands, and nothing of the other person
