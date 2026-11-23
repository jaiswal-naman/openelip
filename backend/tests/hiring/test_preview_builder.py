"""HIR-04 (plan 06-05, task 2): the share_preview of an ask holds confirmed data only, and nothing else.

The candidate is invented (tests/hiring/factories.py). preview.build reads the account and the record on the worker
connection; here the test's own rolled-back connection stands in for it.
"""

import json
from uuid import uuid4

from sqlalchemy import text

from app.hiring import preview
from app.match.api import RoleEvidence, RoleSelection
from tests.hiring import factories as hiring


def selection(candidate, *, cited=None, rows=None):
    """What a role run hands over: one evidence row per requirement, citing the achievement that names the tools."""
    cited = cited if cited is not None else (candidate.record.fact_ids[1],)
    rows = rows or (
        RoleEvidence("Python is required.", "required", "met", tuple(cited)),
        RoleEvidence("Experience with Airflow is a plus.", "preferred", "no_evidence", ()),
    )
    return RoleSelection(candidate.id, uuid4(), "Meets 1 of 1 required item.", tuple(rows))


def add_fact(conn, user, kind, statement, *, confirmed=True, **attributes):
    event = conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', 'fact', 'create', '{}'::jsonb, 'candidate_edit', 1) RETURNING id"""
        ),
        {"u": user},
    ).scalar_one()
    return conn.execute(
        text(
            """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, profile_event_id, confirmed_at)
               VALUES (:u, CAST(:kind AS fact_kind), :statement, CAST(:attributes AS jsonb), 'candidate_edit', :e,
                       CASE WHEN :confirmed THEN now() END)
               RETURNING id"""
        ),
        {
            "u": user,
            "kind": kind,
            "statement": statement,
            "attributes": json.dumps(attributes),
            "confirmed": confirmed,
            "e": event,
        },
    ).scalar_one()


def test_the_preview_has_exactly_the_shape_of_the_contract_and_the_quotes_of_the_cited_facts(conn):
    candidate = hiring.make_candidate(conn, name="Demo Ada", email="demo.ada@example.test")

    built = preview.build(conn, candidate.id, selection(candidate))

    assert set(built) == {"on_accept", "on_employer_yes"}
    assert set(built["on_accept"]) == {"name", "headline", "evidence", "facts"}
    assert built["on_accept"]["name"] == "Demo Ada"
    assert built["on_accept"]["headline"] == "Senior data engineer at Initech Demo, 2019 to 2024."
    assert built["on_accept"]["evidence"] == [
        {
            "requirement": "Python is required.",
            "importance": "required",
            "verdict": "met",
            "record_quotes": ["Built nightly Python and SQL pipelines that cut report time by 40 percent."],
        },
        {
            "requirement": "Experience with Airflow is a plus.",
            "importance": "preferred",
            "verdict": "no_evidence",
            "record_quotes": [],
        },
    ]
    assert built["on_accept"]["facts"] == [
        {"kind": "role", "statement": "Senior data engineer at Initech Demo, 2019 to 2024."}
    ]
    assert built["on_employer_yes"] == {"email": "demo.ada@example.test"}


def test_nothing_that_identifies_a_file_or_a_span_is_in_the_preview(conn):
    candidate = hiring.make_candidate(conn)

    built = preview.build(conn, candidate.id, selection(candidate))

    flat = json.dumps(built)
    assert str(candidate.record.document_id) not in flat
    assert not any(str(fact_id) in flat for fact_id in candidate.record.fact_ids)
    for word in ("document", "span", "provenance", "phone", "source"):
        assert word not in flat.lower().replace("record_quotes", "")


def test_name_and_headline_are_null_when_unknown(conn):
    candidate = hiring.make_candidate(conn)
    conn.execute(text("UPDATE users SET display_name = NULL WHERE id = :u"), {"u": candidate.id})
    conn.execute(
        text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"), {"f": candidate.record.fact_ids[0]}
    )

    built = preview.build(conn, candidate.id, selection(candidate))

    assert built["on_accept"]["name"] is None and built["on_accept"]["headline"] is None
    assert built["on_accept"]["facts"] == []


def test_a_hand_entered_fact_is_quoted_by_its_statement(conn):
    candidate = hiring.make_candidate(conn)
    mine = add_fact(conn, candidate.id, "skill", "Airflow, three years in production.")
    rows = (RoleEvidence("Experience with Airflow is a plus.", "preferred", "met", (mine,)),)

    built = preview.build(conn, candidate.id, selection(candidate, rows=rows))

    assert built["on_accept"]["evidence"][0]["record_quotes"] == ["Airflow, three years in production."]


def test_facts_are_the_confirmed_role_and_education_facts_only(conn):
    candidate = hiring.make_candidate(conn)
    add_fact(conn, candidate.id, "education", "BSc Computer Science, Demo University")
    add_fact(conn, candidate.id, "skill", "Kafka")
    add_fact(conn, candidate.id, "education", "A degree nobody confirmed", confirmed=False)

    built = preview.build(conn, candidate.id, selection(candidate))

    assert {(f["kind"], f["statement"]) for f in built["on_accept"]["facts"]} == {
        ("role", "Senior data engineer at Initech Demo, 2019 to 2024."),
        ("education", "BSc Computer Science, Demo University"),
    }


def test_the_headline_is_the_role_with_no_end_date_else_the_one_that_started_last(conn):
    candidate = hiring.make_candidate(conn)
    conn.execute(
        text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"), {"f": candidate.record.fact_ids[0]}
    )
    add_fact(conn, candidate.id, "role", "Analyst, Old Demo", start_date="2012", end_date="2015")
    add_fact(conn, candidate.id, "role", "Engineer, Newer Demo", start_date="2016-03", end_date="2019")
    assert preview.build(conn, candidate.id, selection(candidate))["on_accept"]["headline"] == "Engineer, Newer Demo"

    add_fact(conn, candidate.id, "role", "Lead, Current Demo", start_date="2020")
    assert preview.build(conn, candidate.id, selection(candidate))["on_accept"]["headline"] == "Lead, Current Demo"


def test_a_cited_fact_that_is_no_longer_usable_gives_none(conn):
    candidate = hiring.make_candidate(conn)
    conn.execute(
        text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"), {"f": candidate.record.fact_ids[1]}
    )

    assert preview.build(conn, candidate.id, selection(candidate)) is None


def test_an_account_that_is_not_active_gives_none(conn):
    candidate = hiring.make_candidate(conn)
    conn.execute(text("UPDATE users SET status = 'suspended' WHERE id = :u"), {"u": candidate.id})

    assert preview.build(conn, candidate.id, selection(candidate)) is None
