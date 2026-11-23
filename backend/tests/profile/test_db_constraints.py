"""The Career Record tables guard themselves (DATA_MODEL 3.4, 4.2, 4.5, 5.3, 5.7): spans, write-once lifecycles,
append-only history, one active constraint per kind, row-level security and erasure.

Rows are made with plain SQL on the superuser test connection, so only the database's own rules are under test. A
statement that must be refused runs inside a savepoint, so the test's transaction survives it.
"""

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

RECORD_TABLES = ("documents", "profile_events", "profile_facts", "constraints", "preferences")
PDF = "application/pdf"
TEXT = "Senior Backend Engineer, Northwind Logistics (Mar 2021 - Present)\nBuilt order pipelines in Python."
VECTOR = "(SELECT ('[' || array_to_string(array_fill(0.1::real, ARRAY[384]), ',') || ']')::vector)"
# Columns a bound Python value cannot reach without an explicit cast.
CASTS = {
    "country_codes": "char(2)[]",
    "cities": "text[]",
    "company_names": "text[]",
    "arrangements": "work_arrangement[]",
    "employment_types": "employment_type[]",
    "seniorities": "seniority[]",
    "attributes": "jsonb",
    "before": "jsonb",
    "after": "jsonb",
}


@contextmanager
def refused(conn, match: str | None = None, code: str | None = None):
    """One statement that must fail; optionally the message or the SQLSTATE it must carry."""
    with pytest.raises(DBAPIError) as caught:
        with conn.begin_nested():
            yield
    if match is not None:
        assert match in str(caught.value.orig)
    if code is not None:
        assert caught.value.orig.sqlstate == code


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def count(conn, source: str, **params) -> int:
    return one(conn, f"SELECT count(*) FROM {source}", **params)


def run(conn, sql: str, **params) -> None:
    conn.execute(text(sql), params)


def insert(conn, table: str, **values) -> UUID:
    """INSERT one row from the given columns and return its id. Dicts become JSON."""
    values = {name: json.dumps(v) if isinstance(v, dict) else v for name, v in values.items()}
    binds = ", ".join(f"CAST(:{name} AS {CASTS[name]})" if name in CASTS else f":{name}" for name in values)
    return one(conn, f"INSERT INTO {table} ({', '.join(values)}) VALUES ({binds}) RETURNING id", **values)


def make_user(conn, name: str = "ada") -> UUID:
    return one(conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=f"{name}-{uuid4().hex[:8]}@example.test")


def make_document(conn, user: UUID, body: str = TEXT) -> UUID:
    return insert(
        conn,
        "documents",
        user_id=user,
        content=b"sealed-bytes",
        mime_type=PDF,
        byte_size=100,
        sha256=b"h" * 32,
        status="parsed",
        extracted_text=body,
        parsed_at=datetime(2026, 11, 23, tzinfo=timezone.utc),
    )


def make_event(conn, user: UUID, **columns) -> UUID:
    """An applied change by default; the columns given replace the defaults, None meaning NULL."""
    values = {
        "event_type": "applied",
        "entity": "fact",
        "operation": "create",
        "source": "candidate_edit",
        "record_version_after": 1,
        "after": {"x": 1},
        **columns,
    }
    return insert(conn, "profile_events", user_id=user, **values)


def make_proposal(conn, user: UUID, origin: str = "chat") -> UUID:
    return make_event(
        conn, user, event_type="proposed", origin=origin, source=None, record_version_after=None, after={"x": 1}
    )


def resolve(conn, user: UUID, proposal: UUID, how: str = "declined") -> UUID:
    return make_event(conn, user, event_type=how, proposal_event_id=proposal, source=None, record_version_after=None)


def make_fact(conn, user: UUID, **columns) -> UUID:
    """A fact written by the candidate (so it needs the event that stated it), unconfirmed unless columns say so."""
    values = {"kind": "other", "statement": "Led a team of four", "source": "candidate_edit", **columns}
    values.setdefault("profile_event_id", make_event(conn, user))
    return insert(conn, "profile_facts", user_id=user, **values)


def make_constraint(conn, user: UUID, kind: str = "remote_policy", **columns) -> UUID:
    values = {"kind": kind, "statement": "Remote only", "source": "candidate_edit", **columns}
    if kind == "remote_policy":
        values.setdefault("arrangements", ["remote"])
    values.setdefault("profile_event_id", make_event(conn, user, entity="constraint"))
    return insert(conn, "constraints", user_id=user, **values)


def make_preference(conn, user: UUID, **columns) -> UUID:
    values = {
        "kind": "target_title",
        "value": "staff engineer",
        "weight": 2,
        "statement": "I want a staff role",
        "source": "candidate_edit",
        **columns,
    }
    values.setdefault("profile_event_id", make_event(conn, user, entity="preference"))
    return insert(conn, "preferences", user_id=user, **values)


def resume_fact(conn, user: UUID, document: UUID, start: int, end: int, quote: str, statement: str = "x") -> UUID:
    return one(
        conn,
        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
           VALUES (:u, 'role', :s, 'resume', :d, int4range(:a, :b), :q) RETURNING id""",
        u=user,
        s=statement,
        d=document,
        a=start,
        b=end,
        q=quote,
    )


def act_as(conn, role: str, user: UUID | None = None) -> None:
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if user else ""})


# ---- spans (invariant 2) ----------------------------------------


def test_a_resume_fact_is_accepted_only_when_its_quote_is_the_document_text_at_its_span(conn):
    user = make_user(conn)
    document = make_document(conn, user)
    quote = TEXT[0:25]
    assert resume_fact(conn, user, document, 0, 25, quote)
    assert one(conn, "SELECT source_quote FROM profile_facts WHERE document_id = :d", d=document) == quote

    with refused(conn, "span_unresolved", "23514"):  # the right words at the wrong place
        resume_fact(conn, user, document, 5, 30, quote)
    with refused(conn, "span_unresolved"):  # words the document does not hold
        resume_fact(conn, user, document, 0, 25, "Chief Executive Officer, Globex")
    with refused(conn, "span_unresolved"):  # a span running past the end reads a shorter text than the quote
        resume_fact(conn, user, document, len(TEXT) - 5, len(TEXT) + 20, TEXT[-5:] + " and more")
    assert count(conn, "profile_facts WHERE user_id = :u", u=user) == 1


def test_a_fact_cannot_quote_another_persons_document(conn):
    owner, other = make_user(conn, "owner"), make_user(conn, "other")
    document = make_document(conn, owner)
    with refused(conn, "span_unresolved"):
        resume_fact(conn, other, document, 0, 25, TEXT[0:25])


def test_a_fact_is_from_a_resume_exactly_when_it_carries_a_document_span_and_quote(conn):
    user = make_user(conn)
    document = make_document(conn, user)
    event = make_event(conn, user)
    with refused(conn, code="23514"):  # source resume, no document
        run(
            conn,
            "INSERT INTO profile_facts (user_id, kind, statement, source) VALUES (:u, 'role', 'x', 'resume')",
            u=user,
        )
    with refused(conn, code="23514"):  # a hand-made fact that claims a span (the span itself is true: TEXT starts "S")
        run(
            conn,
            """INSERT INTO profile_facts (user_id, kind, statement, source, profile_event_id, document_id, source_span,
                                          source_quote)
               VALUES (:u, 'role', 'x', 'candidate_edit', :e, :d, int4range(0, 1), 'S')""",
            u=user,
            e=event,
            d=document,
        )
    with refused(conn, code="23514"):  # a hand-made fact must say which change stated it
        run(
            conn,
            "INSERT INTO profile_facts (user_id, kind, statement, source) VALUES (:u, 'other', 'x', 'candidate_edit')",
            u=user,
        )
    with refused(conn, code="23514"):  # an empty span, with the empty quote that would match it
        resume_fact(conn, user, document, 3, 3, "")
    with refused(conn, code="23514"):  # statements are 1 to 2000 characters
        make_fact(conn, user, statement="x" * 2001)
    with refused(conn, code="23514"):
        make_fact(conn, user, statement="")
    assert make_fact(conn, user, statement="x" * 2000)


# ---- write-once lifecycle columns and no deletes ----------------------------------------


def test_a_fact_can_be_confirmed_once_and_its_words_never_change(conn):
    user = make_user(conn)
    fact = make_fact(conn, user)
    run(conn, "UPDATE profile_facts SET confirmed_at = now() WHERE id = :f", f=fact)
    with refused(conn, "can be set only once", "42501"):
        run(conn, "UPDATE profile_facts SET confirmed_at = now() + interval '1 day' WHERE id = :f", f=fact)
    with refused(conn, "can be set only once"):
        run(conn, "UPDATE profile_facts SET confirmed_at = NULL WHERE id = :f", f=fact)
    with refused(conn, "immutable", "42501"):
        run(conn, "UPDATE profile_facts SET statement = 'Led a team of forty' WHERE id = :f", f=fact)
    with refused(conn, "immutable"):
        run(conn, "UPDATE profile_facts SET kind = 'role' WHERE id = :f", f=fact)
    with refused(conn, "immutable"):
        run(conn, "UPDATE profile_facts SET attributes = CAST('{\"a\": 1}' AS jsonb) WHERE id = :f", f=fact)
    assert one(conn, "SELECT statement FROM profile_facts WHERE id = :f", f=fact) == "Led a team of four"


def test_a_fact_is_confirmed_or_rejected_never_both(conn):
    user = make_user(conn)
    confirmed, rejected = make_fact(conn, user), make_fact(conn, user)
    run(conn, "UPDATE profile_facts SET confirmed_at = now() WHERE id = :f", f=confirmed)
    run(conn, "UPDATE profile_facts SET rejected_at = now() WHERE id = :f", f=rejected)
    with refused(conn, code="23514"):
        run(conn, "UPDATE profile_facts SET rejected_at = now() WHERE id = :f", f=confirmed)
    with refused(conn, code="23514"):
        run(conn, "UPDATE profile_facts SET confirmed_at = now() WHERE id = :f", f=rejected)
    fresh = make_fact(conn, user)
    with refused(conn, code="23514"):
        run(conn, "UPDATE profile_facts SET confirmed_at = now(), rejected_at = now() WHERE id = :f", f=fresh)


def test_a_fact_is_superseded_once_and_its_pointer_is_set_once(conn):
    user = make_user(conn)
    old, new = make_fact(conn, user), make_fact(conn, user, statement="Led a team of five")
    run(conn, "UPDATE profile_facts SET superseded_at = now(), superseded_by = :n WHERE id = :o", n=new, o=old)
    with refused(conn, "can be set only once"):
        run(conn, "UPDATE profile_facts SET superseded_at = now() + interval '1 day' WHERE id = :o", o=old)
    with refused(conn, "can be set only once"):
        run(conn, "UPDATE profile_facts SET superseded_by = :o WHERE id = :o", o=old)


def test_the_pointer_to_the_replacement_may_follow_the_supersede(conn):
    """A constraint is retired before its replacement exists (one active row per kind), so the pointer is set in a
    second step. That has to stay possible."""
    user = make_user(conn)
    old = make_constraint(conn, user)
    run(conn, "UPDATE constraints SET superseded_at = now() WHERE id = :o", o=old)
    new = make_constraint(conn, user, arrangements=["onsite"])
    run(conn, "UPDATE constraints SET superseded_by = :n WHERE id = :o", n=new, o=old)
    assert one(conn, "SELECT superseded_by FROM constraints WHERE id = :o", o=old) == new


@pytest.mark.parametrize(
    ("table", "make"),
    [("profile_facts", make_fact), ("constraints", make_constraint), ("preferences", make_preference)],
)
def test_versioned_rows_are_never_deleted_except_by_erasure(conn, table, make):
    row = make(conn, make_user(conn))
    with refused(conn, "append-only", "42501"):
        run(conn, f"DELETE FROM {table} WHERE id = :i", i=row)
    run(conn, "SELECT set_config('app.maintenance', 'on', true)")
    run(conn, f"DELETE FROM {table} WHERE id = :i", i=row)
    assert count(conn, f"{table} WHERE id = :i", i=row) == 0


@pytest.mark.parametrize("statement", ["UPDATE profile_events SET reason = 'edited'", "DELETE FROM profile_events"])
def test_the_change_log_is_append_only_except_for_erasure(conn, statement):
    make_event(conn, make_user(conn))
    with refused(conn, "append-only", "42501"):
        run(conn, statement)
    run(conn, "SELECT set_config('app.maintenance', 'on', true)")
    run(conn, statement)  # erasure may


# ---- the change log's own rules ----------------------------------------


def test_a_proposal_is_resolved_at_most_once(conn):
    user = make_user(conn)
    proposal = make_proposal(conn, user)
    assert make_event(conn, user, proposal_event_id=proposal, source="confirmed_proposal")  # applied
    with refused(conn, "profile_events_one_resolution", "23505"):
        resolve(conn, user, proposal, "declined")
    with refused(conn, "profile_events_one_resolution"):
        resolve(conn, user, proposal, "expired")
    assert resolve(conn, user, make_proposal(conn, user, "agent"), "declined")
    assert resolve(conn, user, make_proposal(conn, user, "feedback"), "expired")


@pytest.mark.parametrize(
    "columns",
    [
        {"source": None},  # an applied change names its source
        {"record_version_after": None},  # and the version it produced
        {"source": "confirmed_proposal"},  # a confirmed proposal points at its proposal
        {"event_type": "declined", "source": None, "record_version_after": None},  # so does a declined row
        {"event_type": "expired", "source": None, "record_version_after": None},  # and an expired one
        {"event_type": "proposed", "source": None, "record_version_after": None},  # a proposal names its origin
        {"origin": "chat"},  # and only a proposal has one
        {"after": None},  # a change has a before or an after
    ],
)
def test_an_event_row_must_say_who_made_it_and_what_it_resolves(conn, columns):
    with refused(conn, code="23514"):
        make_event(conn, make_user(conn), **columns)


# ---- constraints and preferences ----------------------------------------


def test_one_hard_constraint_per_kind_is_active_at_a_time(conn):
    ada, bob = make_user(conn, "ada"), make_user(conn, "bob")
    first = make_constraint(conn, ada)
    with refused(conn, "constraints_one_active_per_kind", "23505"):
        make_constraint(conn, ada, arrangements=["hybrid"])
    assert make_constraint(conn, bob)  # another person's constraint of the same kind
    assert make_constraint(conn, ada, "employment_type", employment_types=["full_time"], statement="Full time only")
    run(conn, "UPDATE constraints SET superseded_at = now() WHERE id = :i", i=first)
    assert make_constraint(conn, ada, arrangements=["hybrid"])  # the old one is retired, a new one may follow


@pytest.mark.parametrize(
    ("kind", "columns"),
    [
        ("location", {}),
        ("location", {"country_codes": []}),
        ("work_authorization", {}),
        ("remote_policy", {"arrangements": []}),
        ("employment_type", {}),
        ("employment_type", {"employment_types": ["full_time", "unknown"]}),
        ("seniority", {}),
        ("seniority", {"seniorities": ["senior", "unknown"]}),
        ("salary_floor", {"salary_floor": 3000000}),
        ("salary_floor", {"salary_currency": "INR"}),
        ("salary_floor", {"salary_floor": 0, "salary_currency": "INR"}),
        ("excluded_company", {}),
        ("excluded_company", {"company_names": []}),
        ("remote_policy", {"cities": ["pune"]}),  # cities belong to a location only
        ("remote_policy", {"source": "resume"}),  # a constraint is never inferred from a resume
    ],
)
def test_a_constraint_must_hold_what_its_kind_needs(conn, kind, columns):
    with refused(conn, code="23514"):
        make_constraint(conn, make_user(conn), kind, **columns)


def test_a_well_formed_constraint_of_every_kind_is_accepted(conn):
    user = make_user(conn)
    good = {
        "location": {"country_codes": ["IN"], "cities": ["pune"]},
        "work_authorization": {"country_codes": ["IN", "US"]},
        "remote_policy": {"arrangements": ["remote", "hybrid"]},
        "employment_type": {"employment_types": ["full_time", "contract"]},
        "seniority": {"seniorities": ["senior", "staff"]},
        "salary_floor": {"salary_floor": 3000000, "salary_currency": "INR"},
        "excluded_company": {"company_names": ["Globex Example Ltd"]},
    }
    for kind, columns in good.items():
        make_constraint(conn, user, kind, **columns)
    assert count(conn, "constraints WHERE user_id = :u", u=user) == 7
    stored = one(conn, "SELECT country_codes FROM constraints WHERE user_id = :u AND kind = 'location'", u=user)
    assert stored == ["IN"]


@pytest.mark.parametrize("weight", [0, 4, -4])
def test_a_preference_weight_is_between_minus_three_and_three_and_never_zero(conn, weight):
    with refused(conn, code="23514"):
        make_preference(conn, make_user(conn), weight=weight)


def test_an_untyped_dealbreaker_is_always_weight_minus_three(conn):
    user = make_user(conn)
    for weight in (-2, 1, 3):
        with refused(conn, code="23514"):
            make_preference(conn, user, kind="untyped_dealbreaker", weight=weight)
    assert make_preference(conn, user, kind="untyped_dealbreaker", weight=-3)
    for weight in (-3, -1, 1, 3):
        assert make_preference(conn, user, weight=weight)


def test_a_preference_is_never_inferred_from_a_resume(conn):
    with refused(conn, code="23514"):
        make_preference(conn, make_user(conn), source="resume")


# ---- the person, erasure and the header on users ----------------------------------------


def test_the_record_header_on_users_keeps_its_embedding_and_model_together(conn):
    user = make_user(conn)
    assert one(conn, "SELECT record_version FROM users WHERE id = :u", u=user) == 0
    assert one(conn, "SELECT record_confirmed_at FROM users WHERE id = :u", u=user) is None
    with refused(conn, code="23514"):
        run(conn, f"UPDATE users SET record_embedding = {VECTOR} WHERE id = :u", u=user)
    run(
        conn,
        f"UPDATE users SET record_embedding = {VECTOR}, record_embedding_model = 'test-model' WHERE id = :u",
        u=user,
    )


def populate(conn, user: UUID) -> None:
    document = make_document(conn, user)
    resume_fact(conn, user, document, 0, 25, TEXT[0:25])
    make_fact(conn, user)
    make_constraint(conn, user)
    make_preference(conn, user)


def test_deleting_the_person_removes_every_record_row_and_the_embedding_with_the_user_row(conn):
    user, other = make_user(conn, "gone"), make_user(conn, "stays")
    populate(conn, user)
    populate(conn, other)
    run(conn, f"UPDATE users SET record_embedding = {VECTOR}, record_embedding_model = 'm' WHERE id = :u", u=user)
    assert all(count(conn, f"{table} WHERE user_id = :u", u=user) > 0 for table in RECORD_TABLES)

    run(conn, "SELECT set_config('app.maintenance', 'on', true)")  # what erase_user sets for its own transaction
    run(conn, "DELETE FROM users WHERE id = :u", u=user)

    assert all(count(conn, f"{table} WHERE user_id = :u", u=user) == 0 for table in RECORD_TABLES)
    assert count(conn, "users WHERE id = :u", u=user) == 0  # the embedding was a column of this row
    assert all(count(conn, f"{table} WHERE user_id = :u", u=other) > 0 for table in RECORD_TABLES)


def test_erase_user_itself_removes_the_record(conn):
    user = make_user(conn)
    populate(conn, user)
    request = one(
        conn,
        """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
           VALUES (:u, :u, 'deletion', now()) RETURNING id""",
        u=user,
    )
    run(conn, "SELECT erase_user(:u, :r)", u=user, r=request)
    assert all(count(conn, f"{table} WHERE user_id = :u", u=user) == 0 for table in RECORD_TABLES)


# ---- row-level security and grants (DATA_MODEL 5.3, 5.7) ----------------------------------------


def seed_two_people(conn) -> tuple[UUID, UUID]:
    people = make_user(conn, "ada"), make_user(conn, "bob")
    for person in people:
        make_document(conn, person)
        make_fact(conn, person)
        make_constraint(conn, person)
        make_preference(conn, person)
    return people


def test_every_record_table_has_the_two_policies(conn):
    rows = conn.execute(
        text(
            """SELECT c.relname, c.relrowsecurity, array_agg(p.polname ORDER BY p.polname) AS policies
               FROM pg_class c LEFT JOIN pg_policy p ON p.polrelid = c.oid
               WHERE c.relname = ANY(:t) GROUP BY c.relname, c.relrowsecurity"""
        ),
        {"t": list(RECORD_TABLES)},
    ).all()
    assert {row.relname for row in rows} == set(RECORD_TABLES)
    expected = {table: ["candidate_own", "worker_all"] for table in RECORD_TABLES}
    for table in ("profile_facts", "constraints", "preferences"):  # plan 04-01: the match role reads the usable rows
        expected[table] = ["candidate_own", "match_read", "worker_all"]
    assert all(row.relrowsecurity and row.policies == expected[row.relname] for row in rows)


def test_a_signed_in_person_sees_only_their_own_record_rows(conn):
    ada, bob = seed_two_people(conn)
    bobs_event = make_event(conn, bob)  # made before the role changes: only the refused inserts below run as the API
    act_as(conn, "app_api", ada)
    for table in RECORD_TABLES:
        assert conn.execute(text(f"SELECT DISTINCT user_id FROM {table}")).scalars().all() == [ada], table
    with refused(conn, "row-level security", "42501"):  # a row for someone else is refused as well
        insert(
            conn,
            "profile_facts",
            user_id=bob,
            kind="other",
            statement="x",
            source="candidate_edit",
            profile_event_id=bobs_event,
        )
    with refused(conn, "row-level security"):
        make_event(conn, bob)
    with refused(conn, "row-level security"):
        make_document(conn, bob)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # nobody signed in: nothing is visible
    assert all(count(conn, table) == 0 for table in RECORD_TABLES)


def test_the_worker_sees_every_persons_record_rows(conn):
    ada, bob = seed_two_people(conn)
    act_as(conn, "app_worker")
    for table in RECORD_TABLES:
        assert set(conn.execute(text(f"SELECT DISTINCT user_id FROM {table}")).scalars()) >= {ada, bob}, table


def test_the_api_role_cannot_change_a_fact_it_may_not_see(conn):
    ada, bob = seed_two_people(conn)
    bobs_fact = one(conn, "SELECT id FROM profile_facts WHERE user_id = :u", u=bob)
    act_as(conn, "app_api", ada)
    changed = conn.execute(
        text("UPDATE profile_facts SET confirmed_at = now() WHERE id = :f"), {"f": bobs_fact}
    ).rowcount
    assert changed == 0  # the policy hides the row, so the statement finds nothing to change


@pytest.mark.parametrize("role", ["app_api", "app_worker"])
def test_neither_runtime_role_may_rewrite_history_or_delete_versioned_rows(conn, role):
    def allowed(table: str, privilege: str) -> bool:
        return one(conn, "SELECT has_table_privilege(:r, :t, :p)", r=role, t=table, p=privilege)

    assert allowed("profile_events", "SELECT") and allowed("profile_events", "INSERT")
    assert not allowed("profile_events", "UPDATE") and not allowed("profile_events", "DELETE")
    for table in ("profile_facts", "constraints", "preferences"):
        assert allowed(table, "SELECT") and allowed(table, "INSERT") and allowed(table, "UPDATE")
        assert not allowed(table, "DELETE"), table
    assert all(allowed("documents", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE"))
