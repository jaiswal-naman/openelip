"""Revision p4_0401_match_brief: what the database enforces on the decision ledger by itself (plan 04-01).

These rules hold whatever the application does: the citation trigger (MAT-05), the completeness check at commit, the
daily cap (MAT-02), a job version shown once (MAT-07), one nightly run a day, append-only rows, row-level security and
the match login role. Most tests run in the `conn` fixture's transaction on the main test database (already at head),
which is rolled back. The one that needs a real commit uses api_client for its clean-up, and one runs the revision's own
downgrade on a scratch database. Every row is invented (tests/match/factories.py).
"""

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM
from sqlalchemy.exc import DBAPIError

from app.brief import models as brief_models
from app.match import models as match_models
from app.match import privacy as match_privacy
from app.platform import db
from app.trust import privacy
from tests.match.factories import (
    LOCAL_DATE,
    RESUME_TEXT,
    Job,
    Record,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

REPO = Path(__file__).resolve().parents[3]
REVISION = "p4_0401_match_brief"
PREVIOUS = "p3_0303_skill_fks"
DESCRIPTION = "Python is required. SQL is a plus. Mentoring helps."
REQUIREMENTS = [("Python is required.", "required"), ("SQL is a plus.", "preferred"), ("Mentoring helps.", "preferred")]
NEW_ENUMS = (
    "match_trigger",
    "match_run_status",
    "match_outcome",
    "match_reason",
    "unverified_flag",
    "evidence_verdict",
    "feedback_kind",
    "dismiss_reason",
    "brief_channel",
    "brief_status",
    "brief_item_kind",
)
NEW_TABLES = ("match_runs", "match_decisions", "match_evidence", "feedback_events", "briefs", "brief_items")
NEW_FUNCTIONS = (
    "company_key",
    "company_keys",
    "compile_constraints",
    "constraint_violations",
    "unverified_fields",
    "match_evidence_check_citations",
    "match_decisions_check_complete",
)


@contextmanager
def refused(conn: Connection):
    """A statement that must fail, run in a savepoint so the test's transaction survives. Yields pytest's excinfo."""
    savepoint = conn.begin_nested()
    try:
        with pytest.raises(DBAPIError) as caught:
            yield caught
    finally:
        savepoint.rollback()


def sqlstate(caught) -> str:
    return caught.value.orig.sqlstate


def message(caught) -> str:
    return caught.value.orig.diag.message_primary


def constraint(caught) -> str:
    return caught.value.orig.diag.constraint_name


def act_as(conn: Connection, role: str, user_id: UUID | None = None) -> None:
    """Drop the bootstrap superuser's privileges for the rest of the test transaction, as a request does."""
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


def count(conn: Connection, sql: str, **params) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {sql}"), params).scalar_one()


def names(conn: Connection, sql: str, **params) -> list[str]:
    return list(conn.execute(text(sql), params).scalars())


@dataclass(frozen=True)
class World:
    ada: UUID
    bob: UUID
    record: Record
    job: Job  # three requirements
    run: UUID  # ada's nightly run
    decision: UUID  # ada's shown decision for the job, with no evidence yet (the commit-time check is deferred)


@pytest.fixture
def world(conn: Connection) -> World:
    ada, bob = make_user(conn), make_user(conn)
    record = make_record(conn, ada)
    job = make_job(conn, title="Data engineer", description=DESCRIPTION, requirements=REQUIREMENTS, country="IN")
    run = make_run(conn, ada)
    decision = shown(conn, run, ada, job, 1)
    return World(ada, bob, record, job, run, decision)


def shown(conn: Connection, run: UUID, user: UUID, job: Job, rank: int) -> UUID:
    """A shown decision row as it is written before its evidence (the commit-time check is for later)."""
    return make_decision(
        conn, run, user, job, outcome="shown", reasons=("top_ranked",), rank_position=rank, summary="A fit."
    )


def some_jobs(conn: Connection, n: int) -> list[Job]:
    return [
        make_job(conn, title=f"Role {i}", description=f"Skill {i}.", requirements=[(f"Skill {i}.", "required")])
        for i in range(n)
    ]


def add_fact(conn: Connection, user: UUID, document: UUID, state: str) -> UUID:
    """A resume fact that quotes the resume's first line, in one of the lifecycle states the record allows."""
    quote = "Ada Example"
    assert RESUME_TEXT.startswith(quote)  # so the span (0, len) below is a real position in the resume
    lifecycle = {
        "unconfirmed": "NULL, NULL, NULL",
        "rejected": "NULL, now(), NULL",
        "superseded": "now(), NULL, now()",
    }[state]
    return conn.execute(
        text(
            f"""INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote,
                                           confirmed_at, rejected_at, superseded_at)
                VALUES (:u, 'other', :q, 'resume', :d, int4range(0, :n), :q, {lifecycle}) RETURNING id"""
        ),
        {"u": user, "q": quote, "d": document, "n": len(quote)},
    ).scalar_one()


def add_event(conn: Connection, user: UUID, entity: str) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', :e, 'create', '{}'::jsonb, 'candidate_edit', 1) RETURNING id"""
        ),
        {"u": user, "e": entity},
    ).scalar_one()


def cite(
    conn: Connection, decision: UUID, user: UUID, requirement: int, facts: list[UUID], verdict: str = "met"
) -> None:
    conn.execute(
        text(
            """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids)
               VALUES (:d, :u, :r, :v, CAST(:f AS uuid[]))"""
        ),
        {"d": decision, "u": user, "r": requirement, "v": verdict, "f": facts},
    )


def save(conn: Connection, user: UUID, job: Job, decision: UUID | None = None) -> None:
    conn.execute(
        text("INSERT INTO feedback_events (user_id, job_id, match_decision_id, kind) VALUES (:u, :j, :d, 'save')"),
        {"u": user, "j": job.id, "d": decision},
    )


# ---- MAT-05: the citation trigger ---------------------------------------------------------------------------


def test_a_citation_that_resolves_on_both_sides_is_accepted(conn, world):
    cite(conn, world.decision, world.ada, world.job.requirement_ids[0], [world.record.fact_ids[1]])
    cite(conn, world.decision, world.ada, world.job.requirement_ids[1], [], "no_evidence")
    assert count(conn, "match_evidence") == 2


@pytest.mark.parametrize(
    "case", ["another person's fact", "an unconfirmed fact", "a rejected fact", "a superseded fact", "a fact twice"]
)
def test_a_fact_that_does_not_resolve_is_refused_on_the_candidate_side(conn, world, case):
    """The citation names a fact that is not the candidate's current, confirmed one: 23514 citation_unresolved."""
    document = world.record.document_id
    facts = {
        "another person's fact": lambda: [make_record(conn, world.bob).fact_ids[0]],
        "an unconfirmed fact": lambda: [add_fact(conn, world.ada, document, "unconfirmed")],
        "a rejected fact": lambda: [add_fact(conn, world.ada, document, "rejected")],
        "a superseded fact": lambda: [add_fact(conn, world.ada, document, "superseded")],
        "a fact twice": lambda: [world.record.fact_ids[0]] * 2,  # a repeated id fails the count, on purpose
    }[case]()
    with refused(conn) as caught:
        cite(conn, world.decision, world.ada, world.job.requirement_ids[0], facts)
    assert sqlstate(caught) == "23514" and message(caught) == "citation_unresolved: candidate side"


def test_a_requirement_of_another_job_is_refused_on_the_job_side(conn, world):
    other = make_job(
        conn, title="Analyst", description="Excel is required.", requirements=[("Excel is required.", "required")]
    )
    with refused(conn) as caught:
        cite(conn, world.decision, world.ada, other.requirement_ids[0], [])
    assert sqlstate(caught) == "23514" and message(caught) == "citation_unresolved: job side"


def test_a_requirement_of_an_older_content_version_is_refused_on_the_job_side(conn, world):
    """The posting changes (content version 2, one new requirement) and a decision is made for version 2. Version 1's
    requirements are still rows of the same job, but they no longer describe the posting that decision is about."""
    version_two, added = DESCRIPTION + " Pairing is welcome.", "Pairing is welcome."
    conn.execute(
        text("UPDATE jobs SET description = :d, content_version = 2 WHERE id = :j"),
        {"d": version_two, "j": world.job.id},
    )
    start = version_two.find(added)
    conn.execute(
        text(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                             prompt_version)
               VALUES (:j, 2, 1, 'required', 'skill', :t, int4range(:a, :b), 'test')"""
        ),
        {"j": world.job.id, "t": added, "a": start, "b": start + len(added)},
    )
    tomorrow = make_run(conn, world.ada, local_date=LOCAL_DATE + timedelta(days=1))
    current = shown(conn, tomorrow, world.ada, replace(world.job, content_version=2), 1)
    with refused(conn) as caught:
        cite(conn, current, world.ada, world.job.requirement_ids[0], [])
    assert sqlstate(caught) == "23514" and message(caught) == "citation_unresolved: job side"
    with refused(conn) as caught:  # and the decision for version 1 can gain no citation: its version is gone
        cite(conn, world.decision, world.ada, world.job.requirement_ids[0], [])
    assert message(caught) == "citation_unresolved: job side"


def test_an_evidence_row_of_another_person_than_the_decisions_is_refused(conn, world):
    with refused(conn) as caught:
        cite(conn, world.decision, world.bob, world.job.requirement_ids[0], [])
    assert sqlstate(caught) == "23514" and message(caught).startswith("citation_unresolved")


# ---- MAT-03: a shown decision carries a verdict for every requirement, checked at commit ----------------------


def test_a_shown_decision_without_a_verdict_for_every_requirement_is_refused(conn, world):
    cite(conn, world.decision, world.ada, world.job.requirement_ids[0], [world.record.fact_ids[0]])
    cite(conn, world.decision, world.ada, world.job.requirement_ids[1], [], "no_evidence")
    with refused(conn) as caught:  # SET CONSTRAINTS runs, now, the check that a commit would run
        conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
    assert sqlstate(caught) == "23514" and message(caught) == "evidence_incomplete: 2 of 3 requirements have a verdict"
    cite(conn, world.decision, world.ada, world.job.requirement_ids[2], [], "no_evidence")
    conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))  # three of three: accepted


def test_a_shown_decision_for_a_job_with_no_requirements_is_refused(conn):
    ada = make_user(conn)
    bare = make_job(conn, title="Mystery", description="Nothing stated.", requirements=[])
    shown(conn, make_run(conn, ada), ada, bare, 1)
    with refused(conn) as caught:
        conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
    assert message(caught) == "evidence_incomplete: 0 of 0 requirements have a verdict"


def test_a_withheld_decision_needs_no_evidence(conn):
    ada = make_user(conn)
    (job,) = some_jobs(conn, 1)
    make_decision(conn, make_run(conn, ada), ada, job)
    conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))


def test_the_completeness_check_runs_at_commit_and_nothing_half_written_stays(engine, api_client):
    """The real thing, with real commits (api_client empties the tables afterwards)."""
    with engine.begin() as c:
        ada = make_user(c)
        record = make_record(c, ada)
        job = make_job(c, title="Data engineer", description=DESCRIPTION, requirements=REQUIREMENTS)
        run = make_run(c, ada)
    verdicts = [Verdict("met", (record.fact_ids[0],)), Verdict("no_evidence"), Verdict("no_evidence")]

    with pytest.raises(DBAPIError) as caught:  # two verdicts of three: the commit is refused
        with engine.begin() as c:
            decision = shown(c, run, ada, job, 1)
            for requirement, verdict in list(zip(job.requirement_ids, verdicts, strict=True))[:2]:
                cite(c, decision, ada, requirement, list(verdict.fact_ids), verdict.verdict)
    assert sqlstate(caught) == "23514" and message(caught) == "evidence_incomplete: 2 of 3 requirements have a verdict"
    with engine.connect() as c:
        assert count(c, "match_decisions") == 0 and count(c, "match_evidence") == 0

    with engine.begin() as c:  # all three: it commits
        make_shown_decision(c, run, ada, job, verdicts)
    with engine.connect() as c:
        assert count(c, "match_decisions WHERE outcome = 'shown'") == 1 and count(c, "match_evidence") == 3


# ---- MAT-02 and MAT-07: the daily cap and shown-once --------------------------------------------------------


def test_five_shown_decisions_a_day_and_never_a_sixth(conn):
    ada = make_user(conn)
    jobs = some_jobs(conn, 6)
    run, onboarding = make_run(conn, ada), make_run(conn, ada, trigger="onboarding")
    for rank in range(1, 6):
        shown(conn, run, ada, jobs[rank - 1], rank)
    with refused(conn) as caught:  # a sixth position does not exist
        shown(conn, run, ada, jobs[5], 6)
    assert sqlstate(caught) == "23514"
    with refused(conn) as caught:  # a taken position cannot be reused, by this run ...
        shown(conn, run, ada, jobs[5], 3)
    assert sqlstate(caught) == "23505" and constraint(caught) == "match_decisions_daily_cap"
    with refused(conn) as caught:  # ... or by another run of the same local date, whatever started it
        shown(conn, onboarding, ada, jobs[5], 1)
    assert constraint(caught) == "match_decisions_daily_cap"
    shown(conn, make_run(conn, ada, local_date=LOCAL_DATE + timedelta(days=1)), ada, jobs[5], 1)  # a new day


def test_a_job_version_is_shown_once_and_a_new_version_may_be_shown_again(conn):
    ada = make_user(conn)
    (job,) = some_jobs(conn, 1)
    shown(conn, make_run(conn, ada), ada, job, 1)
    tomorrow = make_run(conn, ada, local_date=LOCAL_DATE + timedelta(days=1))
    with refused(conn) as caught:  # tomorrow, another run, the same job at the same version
        shown(conn, tomorrow, ada, job, 1)
    assert sqlstate(caught) == "23505" and constraint(caught) == "match_decisions_shown_once"
    conn.execute(
        text("UPDATE jobs SET description = 'Skill 0. More.', content_version = 2 WHERE id = :j"), {"j": job.id}
    )
    shown(conn, tomorrow, ada, replace(job, content_version=2), 1)
    assert count(conn, "match_decisions WHERE outcome = 'shown'") == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "filtered", "reasons": ("hard_constraint",)},  # filtered, but no constraint named
        {"outcome": "filtered", "violated_kinds": ("location",)},  # constraints named, but no hard_constraint reason
        {"outcome": "withheld", "violated_kinds": ("location",)},  # constraints named on a row that is not filtered
        {"outcome": "withheld", "reasons": ("hard_constraint",)},  # hard_constraint on a row that is not filtered
        {"outcome": "withheld", "rank_position": 1},  # a rank on a job that was not shown
        {"outcome": "shown", "reasons": ("top_ranked",), "summary": "A fit."},  # shown with no rank
        {"outcome": "shown", "reasons": ("top_ranked",), "rank_position": 1},  # shown with no one-sentence summary
        {"outcome": "withheld", "reasons": ("material_change",)},  # a change reason with no change note
        {"outcome": "withheld", "reasons": ()},  # a decision with no reason at all
    ],
    ids=lambda fields: ",".join(f"{k}={v}" for k, v in fields.items()),
)
def test_a_decision_row_must_say_what_its_outcome_needs(conn, fields):
    ada = make_user(conn)
    (job,) = some_jobs(conn, 1)
    with refused(conn) as caught:
        make_decision(conn, make_run(conn, ada), ada, job, **fields)
    assert sqlstate(caught) == "23514"


def test_a_filtered_row_names_the_constraints_it_broke_and_a_withheld_one_names_why(conn):
    ada = make_user(conn)
    first, second = some_jobs(conn, 2)
    run = make_run(conn, ada)
    make_decision(
        conn,
        run,
        ada,
        first,
        outcome="filtered",
        reasons=("hard_constraint",),
        violated_kinds=("location", "seniority"),
    )
    make_decision(conn, run, ada, second, outcome="withheld", reasons=("weak_fit", "below_daily_cap"))
    assert count(conn, "match_decisions") == 2


def test_one_nightly_run_a_day_per_person_and_other_triggers_are_not_limited(conn):
    ada, bob = make_user(conn), make_user(conn)
    make_run(conn, ada)
    with refused(conn) as caught:
        make_run(conn, ada)
    assert sqlstate(caught) == "23505" and constraint(caught) == "match_runs_one_nightly"
    make_run(conn, ada, trigger="onboarding")  # the first matches after onboarding, the same day
    make_run(conn, ada, local_date=LOCAL_DATE + timedelta(days=1))
    make_run(conn, bob)
    assert count(conn, "match_runs WHERE user_id = ANY(:u)", u=[ada, bob]) == 4


def test_a_candidate_run_names_the_record_version_it_read(conn):
    ada = make_user(conn)
    with refused(conn) as caught:
        conn.execute(
            text(
                "INSERT INTO match_runs (trigger, user_id, local_date, ranker_version) VALUES ('nightly', :u, :d, 't')"
            ),
            {"u": ada, "d": LOCAL_DATE},
        )
    assert sqlstate(caught) == "23514"


# ---- append-only, deletion and retention ---------------------------------------------------------------------


def test_the_ledger_evidence_and_feedback_refuse_update_and_delete(conn, world):
    cite(conn, world.decision, world.ada, world.job.requirement_ids[1], [], "no_evidence")
    conn.execute(
        text(
            """INSERT INTO feedback_events (user_id, job_id, match_decision_id, kind, dismiss_reason)
               VALUES (:u, :j, :d, 'dismiss', 'other')"""
        ),
        {"u": world.ada, "j": world.job.id, "d": world.decision},
    )
    for statement in (
        "UPDATE match_decisions SET summary = 'changed'",
        "DELETE FROM match_decisions",
        "UPDATE match_evidence SET note = 'changed'",
        "DELETE FROM match_evidence",
        "UPDATE feedback_events SET comment = 'changed'",
        "DELETE FROM feedback_events",
    ):
        with refused(conn) as caught:
            conn.execute(text(statement))
        assert sqlstate(caught) == "42501", statement
    conn.execute(
        text("UPDATE match_runs SET status = 'failed', error_code = 'x'")
    )  # a run is the pipeline's own record


def test_erase_user_removes_the_person_from_every_new_table_and_nobody_else(conn, world):
    other = make_decision(conn, make_run(conn, world.bob), world.bob, world.job)
    cite(conn, world.decision, world.ada, world.job.requirement_ids[1], [], "no_evidence")
    save(conn, world.ada, world.job, world.decision)
    brief = conn.execute(
        text(
            "INSERT INTO briefs (user_id, local_date, channel, match_run_id) VALUES (:u, :d, 'in_app', :r) RETURNING id"
        ),
        {"u": world.ada, "d": LOCAL_DATE, "r": world.run},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO brief_items (brief_id, position, user_id, kind, match_decision_id)
               VALUES (:b, 1, :u, 'match', :d)"""
        ),
        {"b": brief, "u": world.ada, "d": world.decision},
    )
    request = conn.execute(
        text(
            """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
               VALUES (:u, :u, 'deletion', now() + interval '1 day') RETURNING id"""
        ),
        {"u": world.ada},
    ).scalar_one()
    for table in NEW_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=world.ada) > 0, table

    conn.execute(text("SELECT erase_user(:u, :r)"), {"u": world.ada, "r": request})

    for table in NEW_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=world.ada) == 0, table
    assert count(conn, "match_decisions WHERE id = :d", d=other) == 1  # another person's decision stays
    assert count(conn, "jobs WHERE id = :j", j=world.job.id) == 1  # and so does the public index


def test_purge_expired_removes_old_withheld_and_filtered_decisions_only(conn):
    ada = make_user(conn)
    jobs = some_jobs(conn, 4)
    run = make_run(conn, ada)
    now = conn.execute(text("SELECT now()")).scalar_one()

    def decide(job, days, **fields):
        return make_decision(conn, run, ada, job, created_at=now - timedelta(days=days), **fields)

    old_withheld = decide(jobs[0], 91, outcome="withheld", reasons=("weak_fit",))
    recent_withheld = decide(jobs[1], 89, outcome="withheld", reasons=("weak_fit",))
    old_filtered = decide(jobs[2], 91, outcome="filtered", reasons=("hard_constraint",), violated_kinds=("location",))
    old_shown = decide(jobs[3], 91, outcome="shown", reasons=("top_ranked",), rank_position=1, summary="A fit.")
    act_as(conn, "app_worker")
    swept = conn.execute(text("SELECT purge_expired()")).scalar_one()  # no append-only error: it sets app.maintenance
    conn.execute(text("RESET ROLE"))
    left = set(names(conn, "SELECT id FROM match_decisions"))
    assert left == {recent_withheld, old_shown}  # the shown decision stays with the account at any age
    assert not {old_withheld, old_filtered} & left
    assert swept["match_decisions"] == 2 and swept["export_links"] == 0  # the step that was already there still reports


# ---- the roles -----------------------------------------------------------------------------------------------


def test_the_match_role_is_a_login_without_bypass_and_the_application_connects_as_it(database_url):
    with db.transaction(role="match") as conn:
        assert conn.execute(text("SELECT current_user, current_database()")).one() == (
            "app_match",
            database_url.database,
        )
        role = conn.execute(
            text("SELECT rolcanlogin, rolbypassrls, rolsuper FROM pg_roles WHERE rolname = 'app_match'")
        )
        assert role.one() == (True, False, False)


def test_app_match_reads_only_what_matching_may_use(conn, world):
    """The policies and grants of DATA_MODEL 5.6 and 5.7. A missing policy returns zero rows, so the counts matter."""
    carol = make_user(conn)
    conn.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": carol})
    add_fact(conn, world.ada, world.record.document_id, "unconfirmed")  # not usable
    add_fact(conn, world.ada, world.record.document_id, "rejected")  # not usable
    conn.execute(  # a superseded constraint, and two preferences of which one is superseded: only active ones are read
        text(
            """INSERT INTO constraints (user_id, kind, statement, seniorities, source, profile_event_id, superseded_at)
               VALUES (:u, 'seniority', 'old', CAST('{senior}' AS seniority[]), 'candidate_edit', :e, now())"""
        ),
        {"u": world.ada, "e": add_event(conn, world.ada, "constraint")},
    )
    for superseded in ("NULL", "now()"):
        conn.execute(
            text(
                f"""INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id,
                                             superseded_at)
                    VALUES (:u, 'other', 'v', 1, 's', 'candidate_edit', :e, {superseded})"""
            ),
            {"u": world.ada, "e": add_event(conn, world.ada, "preference")},
        )
    save(conn, world.ada, world.job)
    second = make_job(conn, title="Second", description="Rust.", requirements=[("Rust.", "required")])
    mine = [world.job.id, second.id]

    act_as(conn, "app_match")
    assert count(conn, "jobs WHERE id = ANY(:j)", j=mine) == 2
    assert count(conn, "job_requirements WHERE job_id = ANY(:j)", j=mine) == 4
    assert count(conn, "profile_facts WHERE user_id = :u", u=world.ada) == 3  # the three confirmed, not the two others
    assert (
        count(conn, "constraints WHERE user_id = :u", u=world.ada) == 2
    )  # location and remote policy, not the old one
    assert count(conn, "preferences WHERE user_id = :u", u=world.ada) == 1
    assert count(conn, "users WHERE id = ANY(:u)", u=[world.ada, carol]) == 1  # active accounts only
    assert count(conn, "feedback_events WHERE user_id = :u", u=world.ada) == 1
    assert conn.execute(text("SELECT id, status, timezone FROM users WHERE id = :u"), {"u": world.ada}).one()
    # and its own ledger, which it writes: a run, a decision, and evidence through the citation trigger
    run = conn.execute(
        text(
            """INSERT INTO match_runs (trigger, user_id, local_date, record_version, ranker_version)
               VALUES ('onboarding', :u, :d, 1, 'test') RETURNING id"""
        ),
        {"u": world.ada, "d": LOCAL_DATE},
    ).scalar_one()
    decision = make_decision(conn, run, world.ada, world.job)
    cite(conn, decision, world.ada, world.job.requirement_ids[0], [world.record.fact_ids[0]])
    assert count(conn, "match_evidence WHERE match_decision_id = :d", d=decision) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT count(*) FROM documents",
        "SELECT count(*) FROM consents",
        "SELECT count(*) FROM sessions",
        "SELECT count(*) FROM outbox",
        "SELECT count(*) FROM profile_events",
        "SELECT count(*) FROM data_requests",
        "SELECT count(*) FROM audit_log",
        "SELECT count(*) FROM briefs",
        "SELECT count(*) FROM brief_items",
        "SELECT email FROM users",  # users is open to it by column, and the address is not one
        "UPDATE users SET status = 'suspended'",
        "UPDATE match_decisions SET summary = 'x'",
        "DELETE FROM match_decisions",
        "INSERT INTO feedback_events (user_id, job_id, kind) VALUES (gen_random_uuid(), 1, 'save')",
        "UPDATE profile_facts SET confirmed_at = now()",
        "UPDATE jobs SET title = 'x'",
    ],
)
def test_app_match_cannot_reach_or_change_anything_else(conn, world, statement):
    act_as(conn, "app_match")
    with refused(conn) as caught:
        conn.execute(text(statement))
    # the grant itself says no, before any trigger is asked (a trigger's refusal is also 42501, with another message)
    assert sqlstate(caught) == "42501" and message(caught).startswith("permission denied")


def test_a_signed_in_person_sees_only_their_own_decisions_and_cannot_write_the_ledger(conn, world):
    make_decision(conn, make_run(conn, world.bob), world.bob, world.job)  # bob's, which ada must never see
    save(conn, world.bob, world.job)
    act_as(conn, "app_api", world.ada)
    assert count(conn, "match_decisions") == 1 and count(conn, "match_runs") == 1
    assert count(conn, "feedback_events") == 0  # bob's save is not hers
    save(conn, world.ada, world.job, world.decision)  # her own is
    with refused(conn) as caught:  # a save in someone else's name
        save(conn, world.bob, world.job)
    assert sqlstate(caught) == "42501" and "row-level security" in message(caught)
    with refused(conn) as caught:  # the ledger is written by the pipeline, never by a request
        make_decision(conn, world.run, world.ada, world.job)
    assert sqlstate(caught) == "42501" and message(caught).startswith("permission denied")
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # no signed-in person: nothing at all
    assert count(conn, "match_decisions") == 0


def test_the_worker_writes_the_ledger_for_everyone_but_cannot_rewrite_it(conn, world):
    bobs = make_decision(conn, make_run(conn, world.bob), world.bob, world.job)
    act_as(conn, "app_worker")
    assert count(conn, "match_decisions WHERE id = ANY(:d)", d=[world.decision, bobs]) == 2
    make_decision(conn, world.run, world.ada, make_job(conn, title="Other", description="x", requirements=[]))
    with refused(conn) as caught:  # the grant says no (the append-only trigger behind it is tested above)
        conn.execute(text("UPDATE match_decisions SET summary = 'x'"))
    assert sqlstate(caught) == "42501" and message(caught).startswith("permission denied")


# ---- the export and the registry -------------------------------------------------------------------------------


def test_the_match_exporter_returns_the_persons_rows_in_all_four_tables_and_nobody_elses(conn, world):
    cite(conn, world.decision, world.ada, world.job.requirement_ids[0], [world.record.fact_ids[0]])
    save(conn, world.ada, world.job, world.decision)
    bobs_run = make_run(conn, world.bob)
    make_decision(conn, bobs_run, world.bob, world.job)
    save(conn, world.bob, world.job)

    export = match_privacy.export_match(conn, world.ada)

    assert privacy.exporters()["match"] is match_privacy.export_match  # importing the module registered it
    assert privacy.registry()["match"] == dict.fromkeys(NEW_TABLES[:4], "cascade")
    assert set(export.data) == set(NEW_TABLES[:4]) and export.files == {}
    assert [len(export.data[table]) for table in NEW_TABLES[:4]] == [1, 1, 1, 1]
    assert export.data["match_decisions"][0]["id"] == str(world.decision)
    assert export.data["match_evidence"][0]["fact_ids"] == [str(world.record.fact_ids[0])]
    assert str(world.bob) not in json.dumps(export.data)  # JSON-ready as it stands, and nothing of the other person


# ---- the constraint filter (invariant 1; DATA_MODEL 4.1) -----------------------------------------------------


def apply_function(conn: Connection, function: str, user: UUID, job: Job) -> list[str]:
    return conn.execute(
        text(f"SELECT CAST({function}(j, compile_constraints(:u)) AS text[]) FROM jobs j WHERE j.id = :j"),
        {"u": user, "j": job.id},
    ).scalar_one()


def violations(conn: Connection, user: UUID, job: Job) -> list[str]:
    return apply_function(conn, "constraint_violations", user, job)


def unverified(conn: Connection, user: UUID, job: Job) -> list[str]:
    return apply_function(conn, "unverified_fields", user, job)


def test_a_stated_field_that_contradicts_a_constraint_violates_it_and_an_unstated_one_is_flagged(conn, world):
    """ada's record holds two constraints: India only, and remote or hybrid only."""

    def job(**fields):
        return make_job(conn, title="Role", description="Skill.", requirements=[("Skill.", "required")], **fields)

    unstated, abroad = job(), job(country="US")
    onsite, mixed = job(country="IN", arrangements=("onsite",)), job(country="IN", arrangements=("remote", "onsite"))
    assert violations(conn, world.ada, unstated) == []  # nothing stated, nothing contradicted
    assert unverified(conn, world.ada, unstated) == ["location_not_stated", "arrangement_not_stated"]
    assert violations(conn, world.ada, abroad) == ["location"]
    assert violations(conn, world.ada, onsite) == ["remote_policy"]
    assert violations(conn, world.ada, mixed) == [] and unverified(conn, world.ada, mixed) == ["arrangement_ambiguous"]


def test_an_excluded_company_is_compared_by_its_key_and_a_superseded_constraint_counts_for_nothing(conn, world):
    for kind, column, array_type, value, superseded in (
        ("excluded_company", "company_names", "text[]", '{"Initech Demo, Inc."}', "NULL"),
        ("seniority", "seniorities", "seniority[]", "{senior}", "now()"),  # replaced since: it must not apply
    ):
        conn.execute(
            text(
                f"""INSERT INTO constraints (user_id, kind, statement, {column}, source, profile_event_id,
                                             superseded_at)
                    VALUES (:u, :k, 's', CAST(:v AS {array_type}), 'candidate_edit', :e, {superseded})"""
            ),
            {"u": world.ada, "k": kind, "v": value, "e": add_event(conn, world.ada, "constraint")},
        )
    same = make_job(
        conn, title="Role", description="Skill.", requirements=[("Skill.", "required")], country="IN", seniority="mid"
    )
    assert violations(conn, world.ada, same) == ["excluded_company"]  # "Initech Demo" is "Initech Demo, Inc." by key
    keys = conn.execute(text("SELECT company_key('Initech Demo, Inc.'), company_keys(ARRAY['A B Ltd', 'ab'])")).one()
    assert tuple(keys) == ("initechdemo", ["ab"])
    nobody = make_user(conn)  # no constraints at all: every job passes
    assert violations(conn, nobody, same) == [] and unverified(conn, nobody, same) == []


# ---- the schema is the one DATA_MODEL describes --------------------------------------------------------------


def data_model_labels(type_name: str) -> list[str]:
    source = (REPO / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")
    found = re.search(rf"CREATE TYPE\s+{type_name}\s+AS ENUM\s*\(([^)]*)\)", source)
    assert found, f"DATA_MODEL.md has no CREATE TYPE {type_name}"
    return re.findall(r"'([^']+)'", found.group(1))


@pytest.mark.parametrize("type_name", NEW_ENUMS)
def test_every_new_enum_holds_the_data_model_values_in_order(conn, type_name):
    labels = names(
        conn, "SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:t AS regtype) ORDER BY enumsortorder", t=type_name
    )
    assert labels == data_model_labels(type_name)


@pytest.mark.parametrize("module", [match_models, brief_models], ids=["match", "brief"])
def test_the_table_definitions_list_the_columns_the_migration_made(conn, module):
    for table in module.metadata.tables.values():
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


def test_the_revision_goes_down_one_step_leaving_nothing_and_the_old_purge_comes_back(make_database, alembic_run):
    """The revision's own downgrade on a scratch database: nothing of it is left, purge_expired is exactly what revision
    p3_0301_trust made (text and EXECUTE grant), and the revision goes up again."""
    url = make_database(f"p4down_{os.getpid()}_test")
    engine = create_engine(url.set(drivername="postgresql+psycopg"))
    purge = "SELECT pg_get_functiondef(oid), proacl::text FROM pg_proc WHERE proname = 'purge_expired'"
    try:
        alembic_run("upgrade", url, PREVIOUS)
        with engine.connect() as c:
            before = c.execute(text(purge)).one()
        alembic_run("upgrade", url, REVISION)
        with engine.connect() as c:
            assert c.execute(text(purge)).one() != before
        alembic_run("downgrade", url, PREVIOUS)
        with engine.connect() as c:
            assert c.execute(text(purge)).one() == before
            left = {
                "tables": names(c, "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES)),
                "types": names(
                    c, "SELECT typname FROM pg_type WHERE typname = ANY(:n)", n=[*NEW_ENUMS, "compiled_constraints"]
                ),
                "functions": names(c, "SELECT proname FROM pg_proc WHERE proname = ANY(:n)", n=list(NEW_FUNCTIONS)),
                "policies": names(c, "SELECT policyname FROM pg_policies WHERE policyname LIKE 'match%'"),
                "table grants": names(
                    c, "SELECT table_name FROM information_schema.role_table_grants WHERE grantee = 'app_match'"
                ),
                "column grants": names(
                    c, "SELECT column_name FROM information_schema.column_privileges WHERE grantee = 'app_match'"
                ),
            }
            assert left == {name: [] for name in left}
        alembic_run("upgrade", url, REVISION)
        alembic_run("upgrade", url, REVISION)  # already there: a second upgrade changes nothing
        with engine.connect() as c:
            assert count(c, "pg_tables WHERE tablename = ANY(:n)", n=list(NEW_TABLES)) == len(NEW_TABLES)
            assert names(c, "SELECT version_num FROM alembic_version") == [REVISION]
    finally:
        engine.dispose()


def test_the_revision_sits_on_top_of_the_merged_history():
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config)
    assert script.get_revision(REVISION).down_revision == PREVIOUS
    assert len(script.get_heads()) == 1
