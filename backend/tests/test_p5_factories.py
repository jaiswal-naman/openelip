"""The Phase 5 factories build what they say (plan 05-01), so the plans that use them in waves 2 and 3 start from
rows that are real: confirmed facts that quote the resume, edits the database accepts, a packet, an application with
its history."""

from sqlalchemy import text

from app.profile import api as profile_api
from tests.p5_factories import (
    Edit,
    make_application,
    make_chat_session,
    make_job,
    make_packet,
    make_tailorable_record,
    make_tailored_resume,
    make_user,
)


def count(conn, source: str, **params) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {source}"), params).scalar_one()


def posting(conn):
    return make_job(
        conn,
        title="Data engineer",
        description="Build Python pipelines.",
        requirements=[("Python pipelines", "required")],
    )


def test_a_tailorable_record_is_eleven_confirmed_facts_that_quote_the_resume(conn):
    ada = make_user(conn)
    record = make_tailorable_record(conn, ada)

    facts = {fact["id"]: fact for fact in profile_api.usable_facts(conn, ada)}
    assert set(facts) == {str(fact_id) for fact_id in record.facts.values()} and len(facts) == 11
    kinds = sorted(fact["kind"] for fact in facts.values())
    assert kinds == ["achievement"] * 4 + ["certification", "education", "role", "role", "skill", "skill", "skill"]
    role = facts[str(record.facts["role_initech"])]
    assert role["attributes"] == {
        "employer": "Initech Demo",
        "title": "Senior data engineer",
        "start_date": "2021-03",
        "end_date": "2024-06",
    }
    bullet = facts[str(record.facts["bullet_initech_1"])]
    assert bullet["parent_fact_id"] == str(record.facts["role_initech"])
    # a skill is its own word of the skills line, not the first "Python" in a bullet
    python = facts[str(record.facts["skill_python"])]
    assert python["provenance"]["quote"] == "Python" and python["attributes"] == {"name": "Python"}
    assert (
        python["provenance"]["span"]["start"] > facts[str(record.facts["bullet_globex_2"])]["provenance"]["span"]["end"]
    )
    assert count(conn, "users WHERE id = :u AND record_version = 1 AND record_confirmed_at IS NOT NULL", u=ada) == 1


def test_a_tailored_resume_carries_its_edits_in_order_and_counts_the_discarded_ones(conn):
    ada, job = make_user(conn), None
    record = make_tailorable_record(conn, ada)
    job = posting(conn)
    resume = make_tailored_resume(
        conn,
        ada,
        job,
        edits=[
            Edit("Wrote SQL reports.", "Wrote SQL reports for the finance team.", (record.facts["bullet_globex_1"],)),
            Edit("Built a tool.", "Built a Rust tool.", status="discarded", reason="new_entity"),
            Edit("Led a team.", "Led a team of three.", (record.facts["bullet_initech_2"],), decision="approved"),
        ],
    )
    row = conn.execute(
        text("SELECT status, prompt_version, discarded_count, record_version FROM tailored_resumes WHERE id = :r"),
        {"r": resume},
    ).one()
    assert (row.status, row.discarded_count, row.record_version) == ("ready", 1, 1) and row.prompt_version
    edits = conn.execute(
        text(
            "SELECT ordinal, verifier_status, decision, decided_at IS NOT NULL AS decided FROM resume_edits "
            "WHERE tailored_resume_id = :r ORDER BY ordinal"
        ),
        {"r": resume},
    ).all()
    assert [(e.ordinal, e.verifier_status, e.decision, e.decided) for e in edits] == [
        (1, "passed", "pending", False),
        (2, "discarded", "pending", False),
        (3, "passed", "approved", True),
    ]


def test_a_tailored_resume_that_is_still_generating_has_no_prompt_version_yet(conn):
    ada = make_user(conn)
    make_tailorable_record(conn, ada)
    resume = make_tailored_resume(conn, ada, posting(conn), status="generating")
    row = conn.execute(text("SELECT status, prompt_version FROM tailored_resumes WHERE id = :r"), {"r": resume}).one()
    assert (row.status, row.prompt_version) == ("generating", None)


def test_a_packet_follows_the_latest_tailored_resume_of_the_job_and_a_session_and_application_exist(conn):
    ada = make_user(conn)
    make_tailorable_record(conn, ada)
    job = posting(conn)
    resume = make_tailored_resume(conn, ada, job)
    packet = make_packet(conn, ada, job)
    row = conn.execute(
        text(
            "SELECT tailored_resume_id, status, length(cover_note) AS note, jsonb_array_length(answers) AS answers "
            "FROM application_packets WHERE id = :p"
        ),
        {"p": packet},
    ).one()
    assert (row.tailored_resume_id, row.status, row.answers) == (resume, "ready", 2) and 0 < row.note <= 2000

    assert count(conn, "chat_sessions WHERE id = :s AND kind = 'open'", s=make_chat_session(conn, ada)) == 1
    assert (
        count(conn, "chat_sessions WHERE id = :s AND kind = 'onboarding'", s=make_chat_session(conn, ada, "onboarding"))
        == 1
    )


def test_an_application_has_the_history_its_status_implies(conn):
    ada = make_user(conn)
    job = posting(conn)
    other = make_job(conn, title="Analyst", description="Write SQL.", requirements=[("Write SQL", "required")])
    interview = make_application(conn, ada, job, status="interview")
    applied = make_application(conn, ada, other)
    history = {
        application: conn.execute(
            text("SELECT status FROM application_events WHERE application_id = :a ORDER BY occurred_at"),
            {"a": application},
        )
        .scalars()
        .all()
        for application in (interview, applied)
    }
    assert history == {interview: ["applied", "interview"], applied: ["applied"]}
    rows = {
        row.id: row.status
        for row in conn.execute(text("SELECT id, status FROM applications WHERE user_id = :u"), {"u": ada})
    }
    assert rows == {interview: "interview", applied: "applied"}
