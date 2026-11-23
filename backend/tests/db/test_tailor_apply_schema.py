"""Revision p5_0501_chat_tailor_apply, tailoring and applying (plan 05-01): invariant 3 for resume edits, the
decide-once rule, one current packet per job, the append-only status history, and who may read or erase any of it.

Every test runs in the `conn` fixture's transaction on the main test database, which is rolled back. Rows are invented
(tests/match/factories.py) and written with plain SQL as the bootstrap login, which bypasses row-level security; a
role is taken on with SET LOCAL ROLE when the test is about what that role may do.
"""

import json
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import NamedTuple
from uuid import UUID

import pytest
from sqlalchemy import Connection, text
from sqlalchemy.dialects.postgresql import ARRAY, ENUM
from sqlalchemy.exc import DBAPIError

from app.apply import models as apply_models
from app.apply import privacy as apply_privacy
from app.chat import models as chat_models
from app.chat import privacy as chat_privacy
from app.tailor import models as tailor_models
from app.tailor import privacy as tailor_privacy
from app.trust import privacy
from tests.match.factories import RESUME_FACTS, RESUME_TEXT, Job, Record, make_job, make_record, make_user

# The seven tables this revision made, each with a user_id and each erased by cascade.
PHASE_5_TABLES = (
    "tailored_resumes",
    "resume_edits",
    "application_packets",
    "applications",
    "application_events",
    "chat_sessions",
    "chat_messages",
)
LINE = RESUME_FACTS[2][1]  # "Led a team of three engineers."


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


def count(conn: Connection, source: str, **params) -> int:
    return one(conn, f"SELECT count(*) FROM {source}", **params)


def act_as(conn: Connection, role: str, user_id: UUID | None = None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id) if user_id else ""})


class World(NamedTuple):
    """A person with a confirmed record and a tailored resume of their own for one job."""

    user: UUID
    record: Record
    job: Job
    resume: UUID


def make_posting(conn: Connection) -> Job:
    return make_job(
        conn,
        title="Data engineer",
        description="Build Python pipelines.",
        requirements=[("Python pipelines", "required")],
    )


def make_world(conn: Connection, job: Job) -> World:
    user = make_user(conn)
    record = make_record(conn, user)
    resume = one(
        conn,
        """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version)
           VALUES (:u, :j, :v, :d, 1) RETURNING id""",
        u=user,
        j=job.id,
        v=job.content_version,
        d=record.document_id,
    )
    return World(user, record, job, resume)


def add_fact(
    conn: Connection, world: World, *, confirmed: bool, rejected: bool = False, superseded: bool = False
) -> UUID:
    """One more line of the person's resume as a fact, in whichever state the test needs."""
    start = RESUME_TEXT.find(LINE)
    return one(
        conn,
        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote,
                                      confirmed_at, rejected_at, superseded_at)
           VALUES (:u, 'achievement', :s, 'resume', :doc, int4range(:start, :stop), :s,
                   CASE WHEN :c THEN now() END, CASE WHEN :r THEN now() END, CASE WHEN :x THEN now() END)
           RETURNING id""",
        u=world.user,
        s=LINE,
        doc=world.record.document_id,
        start=start,
        stop=start + len(LINE),
        c=confirmed,
        r=rejected,
        x=superseded,
    )


def add_edit(
    conn: Connection,
    world: World,
    *,
    ordinal: int = 1,
    supporting: Sequence[UUID] = (),
    status: str = "passed",
    reason: str | None = None,
    decision: str = "pending",
    decided: bool = False,
) -> UUID:
    return one(
        conn,
        """INSERT INTO resume_edits (tailored_resume_id, user_id, ordinal, section, before_text, after_text,
                                     supporting_fact_ids, verifier_status, discard_reason, decision, decided_at)
           VALUES (:r, :u, :o, 'experience', 'Led a team.', :after, CAST(:s AS uuid[]), :status, :reason, :decision,
                   CASE WHEN :decided THEN now() END)
           RETURNING id""",
        r=world.resume,
        u=world.user,
        o=ordinal,
        after=LINE,
        s=list(supporting),
        status=status,
        reason=reason,
        decision=decision,
        decided=decided,
    )


def add_packet(
    conn: Connection, world: World, *, status: str = "draft", cover_note: str | None = None, job=None
) -> UUID:
    return one(
        conn,
        """INSERT INTO application_packets (user_id, job_id, tailored_resume_id, status, cover_note, approved_at)
           VALUES (:u, :j, :r, :status, :note, CASE WHEN CAST(:status AS packet_status) = 'approved' THEN now() END)
           RETURNING id""",
        u=world.user,
        j=(job or world.job).id,
        r=world.resume,
        status=status,
        note=cover_note,
    )


def add_application(conn: Connection, world: World, *, job: Job | None = None) -> UUID:
    return one(
        conn,
        """INSERT INTO applications (user_id, job_id, status, status_changed_at) VALUES (:u, :j, 'applied', now())
           RETURNING id""",
        u=world.user,
        j=(job or world.job).id,
    )


def add_event(conn: Connection, application: UUID, user: UUID, status: str = "applied") -> int:
    return one(
        conn,
        """INSERT INTO application_events (application_id, user_id, status, occurred_at)
           VALUES (:a, :u, :s, now()) RETURNING id""",
        a=application,
        u=user,
        s=status,
    )


def add_chat(conn: Connection, user: UUID) -> tuple[UUID, UUID]:
    session = one(conn, "INSERT INTO chat_sessions (user_id, kind) VALUES (:u, 'open') RETURNING id", u=user)
    message = one(
        conn,
        "INSERT INTO chat_messages (session_id, user_id, role, content) VALUES (:s, :u, 'user', 'Hello') RETURNING id",
        s=session,
        u=user,
    )
    return session, message


class Rows(NamedTuple):
    edit: UUID
    packet: UUID
    application: UUID
    session: UUID


def seed_all(conn: Connection, world: World) -> Rows:
    """One row of the person's in each of the seven tables (the tailored resume comes with the world)."""
    edit = add_edit(conn, world, status="discarded", reason="changed_number")
    packet = add_packet(conn, world)
    application = add_application(conn, world)
    add_event(conn, application, world.user)
    session, _ = add_chat(conn, world.user)
    return Rows(edit, packet, application, session)


# ---- invariant 3: an edit rests only on the person's usable facts ------------------------------------------------


def test_a_passed_edit_resting_on_the_persons_own_confirmed_fact_is_stored(conn):
    ada = make_world(conn, make_posting(conn))
    edit = add_edit(conn, ada, supporting=[ada.record.fact_ids[2]])
    assert one(conn, "SELECT decision FROM resume_edits WHERE id = :e", e=edit) == "pending"


@pytest.mark.parametrize("kind", ["another_persons", "unconfirmed", "rejected", "superseded"])
def test_a_passed_edit_resting_on_a_fact_that_is_not_usable_is_refused(conn, kind):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    fact = {
        "another_persons": lambda: bob.record.fact_ids[2],  # confirmed, but Bob's
        "unconfirmed": lambda: add_fact(conn, ada, confirmed=False),
        "rejected": lambda: add_fact(conn, ada, confirmed=False, rejected=True),
        "superseded": lambda: add_fact(conn, ada, confirmed=True, superseded=True),
    }[kind]()
    with refused(conn) as caught:
        add_edit(conn, ada, supporting=[fact])
    assert caught.value.orig.sqlstate == "23514"
    assert caught.value.orig.diag.message_primary.startswith("unsupported_edit")
    assert count(conn, "resume_edits WHERE user_id = :u", u=ada.user) == 0


def test_one_unusable_fact_among_good_ones_refuses_the_whole_edit(conn):
    ada = make_world(conn, make_posting(conn))
    rejected = add_fact(conn, ada, confirmed=False, rejected=True)
    with refused(conn) as caught:
        add_edit(conn, ada, supporting=[ada.record.fact_ids[2], rejected])
    assert caught.value.orig.diag.message_primary.startswith("unsupported_edit")


def test_a_fact_id_listed_twice_is_refused_as_it_is_for_a_citation(conn):
    ada = make_world(conn, make_posting(conn))
    fact = ada.record.fact_ids[2]
    with refused(conn) as caught:
        add_edit(conn, ada, supporting=[fact, fact])
    assert caught.value.orig.diag.message_primary.startswith("unsupported_edit")


def test_a_discarded_edit_needs_no_support_and_names_its_reason(conn):
    ada = make_world(conn, make_posting(conn))
    add_edit(conn, ada, status="discarded", reason="no_supporting_fact")
    assert count(conn, "resume_edits WHERE user_id = :u AND verifier_status = 'discarded'", u=ada.user) == 1


@pytest.mark.parametrize(
    "edit",
    [
        {"status": "passed", "supporting": ()},  # a passed edit must rest on at least one fact
        {"status": "discarded", "reason": None},  # a discarded edit must say why
        {"status": "passed", "reason": "new_entity", "supporting": "own"},  # a passed edit has no discard reason
        {"status": "discarded", "reason": "new_entity", "decided": True, "decision": "rejected"},  # still pending
        {"status": "passed", "supporting": "own", "decided": True},  # a decision time on a pending edit
        {"status": "passed", "supporting": "own", "decision": "approved"},  # a decision with no time
    ],
    ids=[
        "passed-without-support",
        "discarded-without-reason",
        "passed-with-reason",
        "discarded-and-decided",
        "pending-with-time",
        "decided-without-time",
    ],
)
def test_an_edit_that_breaks_a_check_is_refused(conn, edit):
    ada = make_world(conn, make_posting(conn))
    own = [ada.record.fact_ids[2]]
    arguments = {**edit, "supporting": own if edit.get("supporting") == "own" else edit.get("supporting", ())}
    with refused(conn) as caught:
        add_edit(conn, ada, **arguments)
    assert caught.value.orig.sqlstate == "23514"
    assert caught.value.orig.diag.constraint_name.startswith("resume_edits_check")  # a CHECK, not the trigger


def test_an_edit_position_is_used_once_per_tailored_resume(conn):
    ada = make_world(conn, make_posting(conn))
    add_edit(conn, ada, status="discarded", reason="new_entity")
    with refused(conn) as caught:
        add_edit(conn, ada, status="discarded", reason="new_entity")
    assert caught.value.orig.sqlstate == "23505"
    add_edit(conn, ada, ordinal=2, status="discarded", reason="new_entity")


# ---- the candidate decides, once --------------------------------------------------------------------------------


def decide(conn: Connection, edit: UUID, decision: str) -> None:
    conn.execute(
        text("UPDATE resume_edits SET decision = :d, decided_at = now() WHERE id = :e"), {"d": decision, "e": edit}
    )


def test_an_edit_leaves_pending_once_and_then_stays_decided(conn):
    ada = make_world(conn, make_posting(conn))
    approved = add_edit(conn, ada, ordinal=1, supporting=[ada.record.fact_ids[2]])
    rejected = add_edit(conn, ada, ordinal=2, supporting=[ada.record.fact_ids[2]])
    decide(conn, approved, "approved")  # the first decision is allowed: write_once_columns would have refused it
    decide(conn, rejected, "rejected")
    states = conn.execute(text("SELECT id, decision FROM resume_edits WHERE user_id = :u"), {"u": ada.user}).all()
    assert dict(states) == {approved: "approved", rejected: "rejected"}
    with refused(conn) as caught:  # a second decision, in either direction
        decide(conn, approved, "rejected")
    assert caught.value.orig.sqlstate == "42501" and "only once" in caught.value.orig.diag.message_primary
    with refused(conn) as caught:
        decide(conn, rejected, "approved")
    assert caught.value.orig.sqlstate == "42501"


def test_the_time_of_a_decision_cannot_be_moved_afterwards(conn):
    ada = make_world(conn, make_posting(conn))
    edit = add_edit(conn, ada, supporting=[ada.record.fact_ids[2]])
    decide(conn, edit, "approved")
    with refused(conn) as caught:
        conn.execute(
            text("UPDATE resume_edits SET decided_at = decided_at + interval '1 hour' WHERE id = :e"), {"e": edit}
        )
    assert caught.value.orig.sqlstate == "42501"
    conn.execute(
        text("UPDATE resume_edits SET decision = decision WHERE id = :e"), {"e": edit}
    )  # a no-op is not a change


def test_a_discarded_edit_can_never_be_approved(conn):
    ada = make_world(conn, make_posting(conn))
    edit = add_edit(conn, ada, status="discarded", reason="not_entailed")
    with refused(conn) as caught:
        decide(conn, edit, "approved")
    assert caught.value.orig.sqlstate == "23514"  # the CHECK, not the trigger: the decision itself was allowed to move
    assert caught.value.orig.diag.constraint_name.startswith("resume_edits_check")


@pytest.mark.parametrize(
    "column, value",
    [
        ("after_text", "'Led a team of thirty engineers.'"),
        ("before_text", "'Something else.'"),
        ("section", "'summary'"),
        ("ordinal", "9"),
        ("supporting_fact_ids", "'{}'::uuid[]"),
        ("verifier_status", "'discarded'"),
        ("tailored_resume_id", "gen_random_uuid()"),
    ],
)
def test_what_an_edit_says_cannot_be_changed_after_it_is_written(conn, column, value):
    ada = make_world(conn, make_posting(conn))
    edit = add_edit(conn, ada, supporting=[ada.record.fact_ids[2]])
    with refused(conn) as caught:
        conn.execute(text(f"UPDATE resume_edits SET {column} = {value} WHERE id = :e"), {"e": edit})
    assert caught.value.orig.sqlstate == "42501" and "immutable" in caught.value.orig.diag.message_primary


def test_the_trigger_that_freezes_an_edit_is_the_decide_once_function(conn):
    function = one(
        conn,
        """SELECT p.proname FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
           WHERE t.tgrelid = 'resume_edits'::regclass AND t.tgname = 'resume_edits_content_fixed'""",
    )
    assert function == "resume_edits_decide_once"


def test_maintenance_may_still_change_an_edit(conn):
    """erase_user and purge_expired set app.maintenance for their own transaction, as for every guarded table."""
    ada = make_world(conn, make_posting(conn))
    edit = add_edit(conn, ada, supporting=[ada.record.fact_ids[2]])
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
    conn.execute(text("UPDATE resume_edits SET after_text = 'Changed in maintenance.' WHERE id = :e"), {"e": edit})
    assert one(conn, "SELECT after_text FROM resume_edits WHERE id = :e", e=edit) == "Changed in maintenance."


def test_a_person_decides_their_own_edit_through_the_api_role_and_nobody_elses(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    ada_edit = add_edit(conn, ada, supporting=[ada.record.fact_ids[2]])
    bob_edit = add_edit(conn, bob, supporting=[bob.record.fact_ids[2]])
    act_as(conn, "app_api", ada.user)
    decide(conn, ada_edit, "approved")
    updated = conn.execute(
        text("UPDATE resume_edits SET decision = 'approved', decided_at = now() WHERE id = :e"), {"e": bob_edit}
    )
    assert updated.rowcount == 0  # Bob's edit is not visible to Ada, so there is nothing to update
    conn.execute(text("RESET ROLE"))
    assert one(conn, "SELECT decision FROM resume_edits WHERE id = :e", e=bob_edit) == "pending"


# ---- applications and their history -----------------------------------------------------------------------------


def test_a_person_has_one_application_per_job(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    add_application(conn, ada)
    with refused(conn) as caught:
        add_application(conn, ada)
    assert caught.value.orig.sqlstate == "23505"
    add_application(conn, bob)  # the same job, another person


def test_a_status_event_must_name_the_applications_own_person(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    application = add_application(conn, ada)
    add_event(conn, application, ada.user)
    with refused(conn) as caught:  # Bob's id on Ada's application: the composite key refuses it
        add_event(conn, application, bob.user, "interview")
    assert caught.value.orig.sqlstate == "23503"


def test_the_status_history_refuses_update_and_delete_for_every_role(conn):
    ada = make_world(conn, make_posting(conn))
    application = add_application(conn, ada)
    event = add_event(conn, application, ada.user)
    for statement in (
        "UPDATE application_events SET status = 'offer' WHERE id = :e",
        "DELETE FROM application_events WHERE id = :e",
    ):
        with refused(conn) as caught:
            conn.execute(text(statement), {"e": event})
        assert caught.value.orig.sqlstate == "42501" and "append-only" in caught.value.orig.diag.message_primary
    for role in ("app_api", "app_worker"):
        for statement in (
            "UPDATE application_events SET status = 'offer' WHERE id = :e",
            "DELETE FROM application_events WHERE id = :e",
        ):
            savepoint = conn.begin_nested()
            act_as(conn, role, ada.user)
            with pytest.raises(DBAPIError) as caught:
                conn.execute(text(statement), {"e": event})
            savepoint.rollback()
            # the grant answers before the trigger could: only a missing privilege says "permission denied"
            assert caught.value.orig.sqlstate == "42501" and "permission denied" in str(caught.value.orig)


def test_the_api_and_the_worker_may_add_events_to_the_history(conn):
    ada = make_world(conn, make_posting(conn))
    application = add_application(conn, ada)
    act_as(conn, "app_api", ada.user)
    add_event(conn, application, ada.user, "interview")
    conn.execute(text("RESET ROLE"))
    act_as(conn, "app_worker")
    add_event(conn, application, ada.user, "offer")
    conn.execute(text("RESET ROLE"))
    assert count(conn, "application_events WHERE application_id = :a", a=application) == 2


def test_a_status_must_be_one_of_the_five(conn):
    ada = make_world(conn, make_posting(conn))
    application = add_application(conn, ada)
    for status in ("applied", "interview", "offer", "rejected", "no_response"):
        add_event(conn, application, ada.user, status)
    with refused(conn) as caught:
        add_event(conn, application, ada.user, "ghosted")
    assert caught.value.orig.sqlstate == "22P02"


# ---- packets -----------------------------------------------------------------------------------------------------


def test_two_current_packets_for_one_person_and_job_without_a_campaign_are_refused(conn):
    ada = make_world(conn, make_posting(conn))
    add_packet(conn, ada)
    for status in ("draft", "ready"):
        with refused(conn) as caught:
            add_packet(conn, ada, status=status)
        assert caught.value.orig.sqlstate == "23505"
        assert caught.value.orig.diag.constraint_name == "application_packets_one_current_job"


def test_a_superseded_packet_makes_room_for_the_next(conn):
    ada = make_world(conn, make_posting(conn))
    old = add_packet(conn, ada, status="ready")
    conn.execute(text("UPDATE application_packets SET status = 'superseded' WHERE id = :p"), {"p": old})
    add_packet(conn, ada)
    add_packet(conn, ada, status="superseded")  # any number of old ones
    assert count(conn, "application_packets WHERE user_id = :u", u=ada.user) == 3


def test_the_one_current_packet_rule_is_per_person_and_per_job(conn):
    job = make_posting(conn)
    other_job = make_job(
        conn, title="Analyst", description="Write SQL reports.", requirements=[("SQL reports", "required")]
    )
    ada, bob = make_world(conn, job), make_world(conn, job)
    add_packet(conn, ada)
    add_packet(conn, bob)  # another person, the same job
    add_packet(conn, ada, job=other_job)  # the same person, another job


def test_a_packet_with_a_campaign_is_held_to_one_current_packet_per_campaign_instead(conn):
    """campaign_id has no foreign key until Phase 7; the DATA_MODEL index on it is already there."""
    ada = make_world(conn, make_posting(conn))
    campaign = one(conn, "SELECT gen_random_uuid()")
    insert = "INSERT INTO application_packets (user_id, job_id, campaign_id) VALUES (:u, :j, :c)"
    conn.execute(text(insert), {"u": ada.user, "j": ada.job.id, "c": campaign})
    add_packet(conn, ada)  # the campaign-less one for the same job does not collide with it
    with refused(conn) as caught:
        conn.execute(text(insert), {"u": ada.user, "j": ada.job.id, "c": campaign})
    assert caught.value.orig.diag.constraint_name == "application_packets_one_current"


def test_a_cover_note_may_be_two_thousand_characters_and_no_more(conn):
    job = make_posting(conn)
    ada = make_world(conn, job)
    add_packet(conn, ada, cover_note="x" * 2000, status="superseded")
    with refused(conn) as caught:
        add_packet(conn, ada, cover_note="x" * 2001)
    assert caught.value.orig.sqlstate == "23514"


def test_an_approved_packet_carries_the_time_it_was_approved(conn):
    ada = make_world(conn, make_posting(conn))
    insert = "INSERT INTO application_packets (user_id, job_id, status, approved_at) VALUES (:u, :j, 'approved', :t)"
    with refused(conn) as caught:
        conn.execute(text(insert), {"u": ada.user, "j": ada.job.id, "t": None})
    assert caught.value.orig.sqlstate == "23514"
    conn.execute(text(insert), {"u": ada.user, "j": ada.job.id, "t": datetime(2026, 11, 23, 9, 0)})


def test_the_answers_of_a_packet_are_a_json_array(conn):
    ada = make_world(conn, make_posting(conn))
    insert = "INSERT INTO application_packets (user_id, job_id, answers) VALUES (:u, :j, CAST(:a AS jsonb))"
    with refused(conn) as caught:
        conn.execute(text(insert), {"u": ada.user, "j": ada.job.id, "a": "{}"})
    assert caught.value.orig.sqlstate == "23514"


def test_deleting_a_tailored_resume_leaves_its_packet_standing(conn):
    ada = make_world(conn, make_posting(conn))
    packet = add_packet(conn, ada)
    conn.execute(text("DELETE FROM tailored_resumes WHERE id = :r"), {"r": ada.resume})
    assert one(conn, "SELECT tailored_resume_id FROM application_packets WHERE id = :p", p=packet) is None


# ---- who may read them, and what erasure leaves -------------------------------------------------------------------


def insert_in_name_of(table: str, victim: World, rows: Rows, spare_job: Job) -> tuple[str, dict]:
    """A statement that writes one row of `table` in the victim's name, whoever runs it. Each one is valid apart from
    whose name it carries (a job the victim has no packet or application for, a position no edit uses), so the only
    thing left to refuse it is row-level security."""
    mine = {"u": victim.user}
    return {
        "tailored_resumes": (
            """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version)
               VALUES (:u, :j, 1, :d, 1)""",
            {**mine, "j": victim.job.id, "d": victim.record.document_id},
        ),
        "resume_edits": (
            """INSERT INTO resume_edits (tailored_resume_id, user_id, ordinal, section, before_text, after_text,
                                         verifier_status, discard_reason)
               VALUES (:r, :u, 2, 'experience', 'a', 'b', 'discarded', 'new_entity')""",
            {**mine, "r": victim.resume},
        ),
        "application_packets": (
            "INSERT INTO application_packets (user_id, job_id) VALUES (:u, :j)",
            {**mine, "j": spare_job.id},
        ),
        "applications": (
            "INSERT INTO applications (user_id, job_id, status, status_changed_at) VALUES (:u, :j, 'applied', now())",
            {**mine, "j": spare_job.id},
        ),
        "application_events": (
            """INSERT INTO application_events (application_id, user_id, status, occurred_at)
               VALUES (:a, :u, 'offer', now())""",
            {**mine, "a": rows.application},
        ),
        "chat_sessions": ("INSERT INTO chat_sessions (user_id, kind) VALUES (:u, 'open')", mine),
        "chat_messages": (
            "INSERT INTO chat_messages (session_id, user_id, role, content) VALUES (:s, :u, 'user', 'Hi')",
            {**mine, "s": rows.session},
        ),
    }[table]


@pytest.mark.parametrize("table", PHASE_5_TABLES)
def test_a_signed_in_person_sees_only_their_own_rows_and_cannot_write_in_anothers_name(conn, table):
    job = make_posting(conn)
    spare = make_job(
        conn, title="Analyst", description="Write SQL reports.", requirements=[("SQL reports", "required")]
    )
    ada, bob = make_world(conn, job), make_world(conn, job)
    seed_all(conn, ada)
    bobs = seed_all(conn, bob)
    act_as(conn, "app_api", ada.user)
    assert count(conn, table) == 1  # of the two people's rows, only Ada's is visible
    assert count(conn, f"{table} WHERE user_id = :u", u=bob.user) == 0
    sql, params = insert_in_name_of(table, bob, bobs, spare)
    with refused(conn) as caught:
        conn.execute(text(sql), params)
    assert caught.value.orig.sqlstate == "42501" and "row-level security" in str(caught.value.orig)


@pytest.mark.parametrize("table", PHASE_5_TABLES)
def test_the_same_statement_in_the_signed_in_persons_own_name_is_accepted(conn, table):
    """The control for the test above: with Ada as the victim the very same statements go through, so it was the name
    that was refused and not the statement."""
    job = make_posting(conn)
    spare = make_job(
        conn, title="Analyst", description="Write SQL reports.", requirements=[("SQL reports", "required")]
    )
    ada = make_world(conn, job)
    adas = seed_all(conn, ada)
    act_as(conn, "app_api", ada.user)
    sql, params = insert_in_name_of(table, ada, adas, spare)
    conn.execute(text(sql), params)
    assert count(conn, table) == 2


@pytest.mark.parametrize("table", PHASE_5_TABLES)
def test_with_nobody_signed_in_the_api_role_sees_nothing(conn, table):
    ada = make_world(conn, make_posting(conn))
    seed_all(conn, ada)
    act_as(conn, "app_api")
    assert count(conn, table) == 0


def test_the_worker_sees_every_persons_rows(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    seed_all(conn, ada)
    seed_all(conn, bob)
    act_as(conn, "app_worker")
    for table in PHASE_5_TABLES:
        assert count(conn, f"{table} WHERE user_id = ANY(:u)", u=[ada.user, bob.user]) == 2, table


def test_erase_user_removes_one_persons_rows_from_all_seven_tables_and_leaves_the_other(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    seed_all(conn, ada)
    seed_all(conn, bob)
    request = one(
        conn,
        """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
           VALUES (:u, :u, 'deletion', now() + interval '30 days') RETURNING id""",
        u=ada.user,
    )
    act_as(conn, "app_worker")
    conn.execute(text("SELECT erase_user(:u, :r)"), {"u": ada.user, "r": request})
    conn.execute(text("RESET ROLE"))
    for table in PHASE_5_TABLES:
        assert count(conn, f"{table} WHERE user_id = :u", u=ada.user) == 0, table
        assert count(conn, f"{table} WHERE user_id = :u", u=bob.user) == 1, table
    assert count(conn, "jobs WHERE id = :j", j=job.id) == 1  # the job index holds nothing about her


# ---- the registry, the export and the table definitions ---------------------------------------------------------


def test_each_module_registers_its_tables_as_cascade_and_its_exporter_returns_only_the_persons_rows(conn):
    job = make_posting(conn)
    ada, bob = make_world(conn, job), make_world(conn, job)
    adas = seed_all(conn, ada)
    seed_all(conn, bob)

    registered = privacy.registry()
    assert registered["chat"] == {"chat_sessions": "cascade", "chat_messages": "cascade"}
    assert registered["tailor"] == {"tailored_resumes": "cascade", "resume_edits": "cascade"}
    assert registered["apply"] == dict.fromkeys(
        ("application_packets", "applications", "application_events"), "cascade"
    )
    exporters = privacy.exporters()
    assert exporters["chat"] is chat_privacy.export_chat
    assert exporters["tailor"] is tailor_privacy.export_tailor
    assert exporters["apply"] is apply_privacy.export_apply

    chat = chat_privacy.export_chat(conn, ada.user)
    tailor = tailor_privacy.export_tailor(conn, ada.user)
    applied = apply_privacy.export_apply(conn, ada.user)
    for export, tables in ((chat, 2), (tailor, 2), (applied, 3)):
        assert len(export.data) == tables
        assert set(export.files) == ({"files/tailored-resumes/README.txt"} if export is tailor else set())
        assert str(bob.user) not in json.dumps(export.data)  # JSON-ready as it stands, and nothing of the other person
    assert [row["id"] for row in chat.data["chat_sessions"]] == [str(adas.session)]
    assert [row["id"] for row in tailor.data["tailored_resumes"]] == [str(ada.resume)]
    assert [row["id"] for row in tailor.data["resume_edits"]] == [str(adas.edit)]  # a discarded edit is a row about her
    assert [row["id"] for row in applied.data["application_packets"]] == [str(adas.packet)]
    assert [row["id"] for row in applied.data["applications"]] == [str(adas.application)]
    assert len(applied.data["application_events"]) == 1 and len(chat.data["chat_messages"]) == 1


MODELS = {"chat": chat_models, "tailor": tailor_models, "apply": apply_models}


@pytest.mark.parametrize("name", MODELS)
def test_the_table_definitions_list_the_columns_the_migration_made(conn, name):
    for table in MODELS[name].metadata.tables.values():
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


VOCABULARIES = {
    "chat_session_kind": chat_models.CHAT_SESSION_KINDS,
    "chat_role": chat_models.CHAT_ROLES,
    "tailored_status": tailor_models.TAILORED_STATUSES,
    "edit_verifier_status": tailor_models.EDIT_VERIFIER_STATUSES,
    "edit_discard_reason": tailor_models.EDIT_DISCARD_REASONS,
    "edit_decision": tailor_models.EDIT_DECISIONS,
    "application_status": apply_models.APPLICATION_STATUSES,
    "packet_status": apply_models.PACKET_STATUSES,
}


@pytest.mark.parametrize("type_name", VOCABULARIES)
def test_each_vocabulary_in_the_models_is_the_enum_the_migration_made_in_the_same_order(conn, type_name):
    labels = conn.execute(
        text("SELECT enumlabel FROM pg_enum WHERE enumtypid = CAST(:t AS regtype) ORDER BY enumsortorder"),
        {"t": type_name},
    ).scalars()
    assert tuple(labels) == VOCABULARIES[type_name]


def test_the_finish_reasons_in_the_chat_models_are_the_ones_the_check_allows(conn):
    user = make_user(conn)
    session = one(conn, "INSERT INTO chat_sessions (user_id, kind) VALUES (:u, 'open') RETURNING id", u=user)

    def accepted(reason: str) -> bool:
        savepoint = conn.begin_nested()
        try:
            conn.execute(
                text(
                    """INSERT INTO chat_messages (session_id, user_id, role, content, finish_reason)
                       VALUES (:s, :u, 'assistant', 'x', :r)"""
                ),
                {"s": session, "u": user, "r": reason},
            )
            return True
        except DBAPIError:
            return False
        finally:
            savepoint.rollback()  # leave nothing behind either way

    tried = (*chat_models.FINISH_REASONS, "done", "error", "")
    assert tuple(reason for reason in tried if accepted(reason)) == chat_models.FINISH_REASONS
