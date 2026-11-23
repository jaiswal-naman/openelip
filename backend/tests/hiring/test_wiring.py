"""The hiring module is wired once into both composition roots, the deletion registry and the export, and its table
definitions say what the migration made (plan 06-01). Rows are invented (tests/hiring/world.py)."""

import importlib

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM

from app.hiring import api as hiring_api
from app.hiring import models
from app.hiring import privacy as hiring_privacy
from app.platform import auth
from app.trust import privacy
from tests.hiring.world import PREVIEW, accept, ask, candidate, make_org, open_role, shortlist, signal
from tests.match.factories import make_user

SEVEN = {
    "org_members": "cascade",
    "introductions": "cascade",
    "shortlist_entries": "cascade",
    "interest_signals": "cascade",
    "organizations": "set_null",  # created_by is set to NULL: the organization stays when its creator goes
    "roles": "set_null",
    "role_requirements": "no_personal_data",
}
VOCABULARIES = {
    "org_role": models.ORG_ROLES,
    "member_status": models.MEMBER_STATUSES,
    "role_status": models.ROLE_STATUSES,
    "introduction_status": models.INTRODUCTION_STATUSES,
    "employer_answer": models.EMPLOYER_ANSWERS,
    "signal_side": models.SIGNAL_SIDES,
    "work_arrangement": models.WORK_ARRANGEMENTS,
    "employment_type": models.EMPLOYMENT_TYPES,
    "seniority": models.SENIORITIES,
    "visa_sponsorship": models.VISA_SPONSORSHIPS,
    "requirement_importance": models.REQUIREMENT_IMPORTANCES,
    "requirement_kind": models.REQUIREMENT_KINDS,
}


# ---- the roots ---------------------------------------------------------------------------------------------------


def test_both_roots_load_the_hiring_module_and_the_worker_keeps_the_ingest_name():
    """Plan 06-01 wired the module in once, so the plans that fill it never touch a root again."""
    from app import main, worker
    from app.hiring import jobs as hiring_jobs

    assert main.hiring_privacy is hiring_privacy and worker.hiring_privacy is hiring_privacy
    assert worker.hiring_jobs is hiring_jobs and worker.hiring_jobs.__name__ == "app.hiring.jobs"
    assert worker.jobs.__name__ == "app.ingest.jobs"  # a plain `from app.hiring import jobs` would have taken the name
    assert {hiring_jobs.ask_jobs.__name__, hiring_jobs.org_mail.__name__, hiring_jobs.introduction_mail.__name__} == {
        "app.hiring.ask_jobs",
        "app.hiring.org_mail",
        "app.hiring.introduction_mail",
    }  # the worker loads the handlers and mail templates of the plans that fill them
    assert [
        module.__name__
        for module in (
            main.hiring_org_routes,
            main.hiring_role_routes,
            main.hiring_introduction_routes,
            main.hiring_signal_routes,
        )
    ] == [f"app.hiring.{name}_routes" for name in ("org", "role", "introduction", "signal")]


def test_every_hiring_module_imports_without_a_database():
    for name in (
        "app.hiring",
        "app.hiring.models",
        "app.hiring.api",
        "app.hiring.queries",
        "app.hiring.access",
        "app.hiring.share",
        "app.hiring.errors",
        "app.hiring.privacy",
        "app.hiring.jobs",
        "app.hiring.org_routes",
        "app.hiring.role_routes",
        "app.hiring.introduction_routes",
        "app.hiring.signal_routes",
        "app.hiring.org_mail",
        "app.hiring.introduction_mail",
        "app.hiring.ask_jobs",
    ):
        importlib.import_module(name)


def test_the_account_endpoints_get_the_memberships_from_the_hiring_module(api_client):
    """The platform may not import the hiring module, so the API's root hands it over (ARCHITECTURE 3.3)."""
    assert auth.hooks["organizations"] is hiring_api.my_organizations


# ---- the deletion registry and the export ------------------------------------------------------------------------


def test_the_seven_tables_are_registered_with_what_erasure_does_to_each():
    importlib.import_module("app.main")
    importlib.import_module("app.worker")
    assert privacy.registry()["hiring"] == SEVEN
    assert "hiring" in privacy.exporters()


def test_the_export_holds_the_persons_own_rows_and_no_one_elses_names(conn: Connection):
    owner, colleague = make_user(conn), make_user(conn)
    org = make_org(conn, created_by=owner)
    role = open_role(conn, org, created_by=owner)
    cat, other = candidate(conn), candidate(conn)
    intro = ask(conn, role, cat)
    accept(conn, intro, role, cat)
    entry = shortlist(conn, intro)
    ask(conn, role, other)
    conn.execute(
        text(
            """UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = 'Strong fit',
                                        employer_answered_by = :o WHERE id = :i"""
        ),
        {"o": owner, "i": intro},
    )
    own_signal = signal(conn, "candidate", cat, org, role.id, sent_by=cat)
    org_signal = signal(conn, "organization", cat, org, role.id, intro=intro, sent_by=owner)
    conn.execute(
        text(
            """INSERT INTO org_members (organization_id, user_id, role, status, joined_at, invited_by)
               VALUES (:o, :u, 'member', 'active', now(), :b)"""
        ),
        {"o": org, "u": colleague, "b": owner},
    )

    mine = hiring_privacy.export_hiring(conn, cat).data
    assert set(mine) == {"org_members", "introductions", "shortlist_entries", "interest_signals"}
    assert [row["id"] for row in mine["introductions"]] == [str(intro)]  # the other candidate's ask is not here
    assert mine["introductions"][0]["share_preview"] == PREVIEW and mine["introductions"][0]["employer_answer"] == "yes"
    assert [row["id"] for row in mine["shortlist_entries"]] == [str(entry)]
    assert {row["id"] for row in mine["interest_signals"]} == {str(own_signal), str(org_signal)}
    assert mine["org_members"] == []
    # the people who answered, sent or invited are other people's identifiers: they stay out of the archive
    for table, column in (("introductions", "employer_answered_by"), ("interest_signals", "sent_by")):
        assert all(column not in row for row in mine[table])

    theirs = hiring_privacy.export_hiring(conn, colleague).data
    assert [row["user_id"] for row in theirs["org_members"]] == [str(colleague)]
    assert "invited_by" not in theirs["org_members"][0]
    assert theirs["introductions"] == theirs["shortlist_entries"] == theirs["interest_signals"] == []


# ---- the definitions --------------------------------------------------------------------------------------------


def test_the_table_definitions_list_the_columns_the_migration_made(conn: Connection):
    assert set(models.metadata.tables) == set(SEVEN)
    for table in models.metadata.tables.values():
        rows = conn.execute(
            text(
                """SELECT column_name, is_nullable = 'YES', udt_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = :t"""
            ),
            {"t": table.name},
        ).all()
        assert {(r[0], r[1]) for r in rows} == {(c.name, bool(c.nullable)) for c in table.columns}, table.name
        types = {(r[0], r[2]) for r in rows}
        for column in table.columns:  # an enum column names the type the migration made
            if isinstance(column.type, ENUM):
                assert (column.name, column.type.name) in types, (table.name, column.name)
            elif isinstance(column.type, ARRAY) and isinstance(column.type.item_type, ENUM):
                assert (column.name, "_" + column.type.item_type.name) in types, (table.name, column.name)


@pytest.mark.parametrize("type_name", list(VOCABULARIES))
def test_each_vocabulary_is_the_enum_the_database_has(conn: Connection, type_name: str):
    labels = list(
        conn.execute(
            text("SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:t AS regtype) ORDER BY enumsortorder"),
            {"t": type_name},
        ).scalars()
    )
    assert list(VOCABULARIES[type_name]) == labels
