"""The Career Record's operations (REC-02 to REC-05; plan 03-04): facts, hard constraints, soft preferences.

Each operation writes exactly one applied change-log row and raises record_version by one, atomically. The service
runs on the superuser test connection (rolled back after each test), so what is under test is the service and the
rules the database adds, not row-level security (test_db_constraints and test_routes_record cover that).
"""

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.profile import events, models, service
from app.profile.errors import BadCursor, ConstraintNotEnforceable, InvalidField, RecordNotFound, StateConflict
from app.profile.service import Via

TEXT = "Senior Backend Engineer, Northwind Logistics (Mar 2021 - Present)\nBuilt order pipelines in Python."
RECORD_TABLES = ("documents", "profile_events", "profile_facts", "constraints", "preferences")
REMOTE = ("remote_policy", {"arrangements": ["remote"]}, "Remote only")


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def make_user(conn, name: str = "ada") -> UUID:
    return one(conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=f"{name}-{uuid4().hex[:8]}@example.test")


def unconfirmed_fact(conn, user: UUID, start: int = 0, end: int = 25) -> UUID:
    """A fact extracted from a resume, waiting for review (written as plan 03-05's parse will write it)."""
    document = one(
        conn,
        """INSERT INTO documents (user_id, content, mime_type, byte_size, sha256, status, extracted_text, parsed_at)
           VALUES (:u, :c, 'application/pdf', 100, :h, 'parsed', :t, now()) RETURNING id""",
        u=user,
        c=b"sealed",
        h=b"h" * 32,
        t=TEXT,
    )
    return one(
        conn,
        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
           VALUES (:u, 'role', :q, 'resume', :d, int4range(:a, :b), :q) RETURNING id""",
        u=user,
        d=document,
        a=start,
        b=end,
        q=TEXT[start:end],
    )


def state(conn, user: UUID) -> dict[str, Any]:
    """Everything an operation could change, so a test can say what did and did not."""
    counts = {t: one(conn, f"SELECT count(*) FROM {t} WHERE user_id = :u", u=user) for t in RECORD_TABLES}
    header = conn.execute(
        text("SELECT record_version, record_confirmed_at FROM users WHERE id = :u"), {"u": user}
    ).one()
    return {**counts, "version": header.record_version, "confirmed_at": header.record_confirmed_at}


def last_event(conn, user: UUID):
    return conn.execute(
        text("SELECT * FROM profile_events WHERE user_id = :u ORDER BY created_at DESC, id DESC LIMIT 1"), {"u": user}
    ).one()


@pytest.fixture
def user(conn) -> UUID:
    return make_user(conn)


# ---- one event, one version, atomically ----------------------------------------------------------
# name -> (setup, what the event must say). Setup returns the operation to time; (entity, operation, before is NULL,
# after is NULL) is what the one new change-log row must hold.

Case = tuple[Callable[[Any, UUID], Callable[[], service.Applied]], tuple[str, str, bool, bool]]


def _confirmed_fact(conn, user):
    return service.add_fact(conn, user, "skill", "Kubernetes").row["id"]


def _active_remote(conn, user):
    return service.set_constraint(conn, user, *REMOTE).row["id"]


def _active_preference(conn, user):
    return service.add_preference(conn, user, "target_title", "A staff role", value="staff engineer", weight=2).row[
        "id"
    ]


CASES: dict[str, Case] = {
    "add_fact": (lambda c, u: lambda: service.add_fact(c, u, "skill", "Kubernetes"), ("fact", "create", True, False)),
    "confirm_fact": (
        lambda c, u: lambda f=unconfirmed_fact(c, u): service.confirm_fact(c, u, f),
        ("fact", "confirm", False, False),
    ),
    "correct_unconfirmed_fact": (
        lambda c, u: lambda f=unconfirmed_fact(c, u): service.correct_fact(c, u, f, "Led a team of four"),
        ("fact", "update", False, False),
    ),
    "correct_confirmed_fact": (
        lambda c, u: lambda f=_confirmed_fact(c, u): service.correct_fact(c, u, UUID(f), "Kubernetes and Helm"),
        ("fact", "update", False, False),
    ),
    "reject_unconfirmed_fact": (
        lambda c, u: lambda f=unconfirmed_fact(c, u): service.reject_fact(c, u, f),
        ("fact", "reject", False, False),
    ),
    "reject_confirmed_fact": (
        lambda c, u: lambda f=_confirmed_fact(c, u): service.reject_fact(c, u, UUID(f)),
        ("fact", "remove", False, True),
    ),
    "set_constraint": (
        lambda c, u: lambda: service.set_constraint(c, u, *REMOTE),
        ("constraint", "create", True, False),
    ),
    "replace_constraint": (
        lambda c, u: (
            lambda _=_active_remote(c, u): service.set_constraint(
                c, u, "remote_policy", {"arrangements": ["hybrid"]}, "Hybrid"
            )
        ),
        ("constraint", "update", False, False),
    ),
    "change_constraint": (
        lambda c, u: lambda i=_active_remote(c, u): service.change_constraint(c, u, UUID(i), {"statement": "Remote!"}),
        ("constraint", "update", False, False),
    ),
    "remove_constraint": (
        lambda c, u: lambda i=_active_remote(c, u): service.remove_constraint(c, u, UUID(i)),
        ("constraint", "remove", False, True),
    ),
    "add_preference": (
        lambda c, u: lambda: service.add_preference(c, u, "industry", "Fintech", value="fintech", weight=1),
        ("preference", "create", True, False),
    ),
    "change_preference": (
        lambda c, u: lambda i=_active_preference(c, u): service.change_preference(c, u, UUID(i), {"weight": 3}),
        ("preference", "update", False, False),
    ),
    "remove_preference": (
        lambda c, u: lambda i=_active_preference(c, u): service.remove_preference(c, u, UUID(i)),
        ("preference", "remove", False, True),
    ),
}


@pytest.mark.parametrize("name", list(CASES))
def test_every_operation_writes_one_applied_event_and_raises_the_version_by_one(conn, user, name):
    setup, (entity, operation, no_before, no_after) = CASES[name]
    run = setup(conn, user)
    before = state(conn, user)

    applied = run()

    after = state(conn, user)
    assert after["version"] == before["version"] + 1
    assert after["profile_events"] == before["profile_events"] + 1
    event = last_event(conn, user)
    assert (event.event_type, event.entity, event.operation) == ("applied", entity, operation)
    assert (event.before is None, event.after is None) == (no_before, no_after)
    assert (event.source, event.proposal_event_id) == ("candidate_edit", None)
    assert event.record_version_after == after["version"]
    assert str(event.entity_id) == applied.row["id"] and applied.entity == entity
    assert (applied.event_id, applied.record_version) == (event.id, after["version"])


@pytest.mark.parametrize("name", ["add_fact", "confirm_fact", "set_constraint", "replace_constraint", "add_preference"])
def test_a_failure_after_the_event_was_written_leaves_neither_the_event_nor_the_version(conn, user, monkeypatch, name):
    setup, _ = CASES[name]
    run = setup(conn, user)
    before = state(conn, user)
    real = service._apply

    def apply_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("the rows after the event failed")

    monkeypatch.setattr(service, "_apply", apply_then_fail)
    with pytest.raises(RuntimeError):
        run()
    assert state(conn, user) == before  # and the connection is still usable: state() just ran on it


def test_the_first_fact_the_person_confirms_sets_record_confirmed_at_and_only_that_one(conn, user):
    assert state(conn, user)["confirmed_at"] is None
    service.set_constraint(conn, user, *REMOTE)
    service.add_preference(conn, user, "industry", "Fintech", value="fintech", weight=1)
    assert state(conn, user)["confirmed_at"] is None  # nothing to match yet: no confirmed fact

    service.confirm_fact(conn, user, unconfirmed_fact(conn, user))
    first = state(conn, user)["confirmed_at"]
    assert first is not None
    service.add_fact(conn, user, "skill", "Kubernetes")
    assert state(conn, user)["confirmed_at"] == first  # set once, by the first confirmation


@pytest.mark.parametrize("name", ["add_fact", "correct_unconfirmed_fact"])
def test_a_fact_the_candidate_writes_also_counts_as_the_first_confirmation(conn, user, name):
    CASES[name][0](conn, user)()
    assert state(conn, user)["confirmed_at"] is not None


def test_operations_on_one_persons_record_run_one_at_a_time(engine):
    """A second operation waits for the first and then sees what it did, so it answers 409 and not a database error."""
    with engine.begin() as setup:
        user = make_user(setup)
        fact = unconfirmed_fact(setup, user)
    outcome: list[BaseException | None] = []
    first = engine.connect()
    try:
        first.begin()
        service.confirm_fact(first, user, fact)  # holds the person's lock until it commits

        def second() -> None:
            with engine.connect() as other:
                other.begin()
                try:
                    service.confirm_fact(other, user, fact)
                    outcome.append(None)
                except BaseException as error:  # the thread reports whatever happened
                    outcome.append(error)
                finally:
                    other.rollback()

        thread = threading.Thread(target=second)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive() and outcome == []  # still waiting for the first
        first.commit()
        thread.join(timeout=10)
    finally:
        first.close()
        with engine.begin() as cleanup:
            cleanup.execute(text("TRUNCATE users CASCADE"))  # the person was committed: remove them
    assert not thread.is_alive() and len(outcome) == 1
    assert isinstance(outcome[0], StateConflict) and outcome[0].state == "confirmed"


@pytest.mark.parametrize("role", ["app_api", "app_worker"])
def test_every_operation_works_under_the_roles_that_run_it_row_level_security_included(conn, role):
    """The API acts as one person (policies filter every row); the worker acts for everyone (a chat agent, a job)."""
    person = make_user(conn)
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(person) if role == "app_api" else ""})

    fact = service.add_fact(conn, person, "skill", "Kubernetes")
    service.correct_fact(conn, person, UUID(fact.row["id"]), "Kubernetes and Helm")
    service.set_constraint(conn, person, *REMOTE)
    service.set_constraint(conn, person, "remote_policy", {"arrangements": ["hybrid"]}, "Hybrid")
    pref = service.add_preference(conn, person, "untyped_dealbreaker", "No remote work")
    service.remove_preference(conn, person, UUID(pref.row["id"]))
    proposal = events.propose_change(
        conn, person, origin="agent", entity="fact", operation="create", after={"kind": "skill", "statement": "Go"}
    )
    events.confirm(conn, person, proposal)
    events.decline(
        conn,
        person,
        events.propose_change(
            conn,
            person,
            origin="chat",
            entity="constraint",
            operation="create",
            after={"kind": "location", "country_codes": ["IN"], "statement": "India"},
        ),
    )

    assert service.record_header(conn, person).record_version == 7
    assert [f["statement"] for f in service.list_facts(conn, person, status="confirmed")[0]] == [
        "Kubernetes and Helm",
        "Go",
    ]
    assert [c["arrangements"] for c in service.list_constraints(conn, person)] == [["hybrid"]]


# ---- facts ---------------------------------------------------------------------------------------


def test_a_fact_moves_only_along_its_lifecycle(conn, user):
    unconfirmed, rejected, retired = (unconfirmed_fact(conn, user) for _ in range(3))
    service.confirm_fact(conn, user, unconfirmed)
    service.reject_fact(conn, user, rejected)
    service.correct_fact(conn, user, retired, "Led a team of four")

    for operation in (
        lambda f: service.confirm_fact(conn, user, f),
        lambda f: service.correct_fact(conn, user, f, "x"),
        lambda f: service.reject_fact(conn, user, f),
    ):
        for fact, current in ((rejected, "rejected"), (retired, "superseded")):
            with pytest.raises(StateConflict) as refused:
                operation(fact)
            assert refused.value.details == {"state": current}
    with pytest.raises(StateConflict) as refused:
        service.confirm_fact(conn, user, unconfirmed)  # already confirmed
    assert refused.value.state == "confirmed"


def test_a_correction_is_a_new_confirmed_fact_in_the_candidates_words_and_the_old_one_is_superseded(conn, user):
    old = unconfirmed_fact(conn, user)
    applied = service.correct_fact(conn, user, old, "  Backend engineer at Northwind  ", reason="Title was wrong")

    new = service.fact_row(conn, user, UUID(applied.row["id"]))
    assert (new.status, new.source, new.statement, new.attributes) == (
        "confirmed",
        "candidate_edit",
        "Backend engineer at Northwind",
        {},
    )
    assert new.document_id is None and new.kind == "role"
    superseded = service.fact_row(conn, user, old)
    assert superseded.status == "superseded"
    assert one(conn, "SELECT superseded_by FROM profile_facts WHERE id = :f", f=old) == UUID(applied.row["id"])
    event = last_event(conn, user)
    assert event.reason == "Title was wrong"
    assert (
        event.before["provenance"]["quote"] == TEXT[0:25]
    )  # the log keeps the original words and where they came from
    assert event.after["statement"] == "Backend engineer at Northwind" and event.after["provenance"] is None
    assert one(conn, "SELECT profile_event_id FROM profile_facts WHERE id = :f", f=UUID(applied.row["id"])) == event.id


def test_rejecting_a_confirmed_fact_retires_it_and_the_log_calls_it_a_removal(conn, user):
    fact = UUID(service.add_fact(conn, user, "skill", "Kubernetes").row["id"])
    applied = service.reject_fact(conn, user, fact, reason="Only used it in a course")
    assert applied.row["status"] == "superseded"
    assert service.fact_row(conn, user, fact).status == "superseded"
    event = last_event(conn, user)
    assert (event.operation, event.after, event.reason) == ("remove", None, "Only used it in a course")


def test_a_hand_written_fact_is_confirmed_on_creation_with_no_source_span(conn, user):
    applied = service.add_fact(conn, user, "skill", "  Kubernetes  ")
    row = service.fact_row(conn, user, UUID(applied.row["id"]))
    assert (row.status, row.source, row.statement, row.document_id, row.span_start) == (
        "confirmed",
        "candidate_edit",
        "Kubernetes",
        None,
        None,
    )
    assert applied.row["provenance"] is None and applied.row["attributes"] == {}


@pytest.mark.parametrize(
    ("kind", "statement", "field", "issue"),
    [
        ("astronaut", "Kubernetes", "kind", "invalid_value"),
        ("skill", "", "statement", "required"),
        ("skill", "   ", "statement", "required"),
        ("skill", "x" * 2001, "statement", "too_long"),
        ("skill", "bad\x00text", "statement", "invalid_characters"),
        ("skill", 42, "statement", "invalid_value"),
    ],
)
def test_a_bad_fact_is_refused_and_writes_nothing(conn, user, kind, statement, field, issue):
    before = state(conn, user)
    with pytest.raises(InvalidField) as refused:
        service.add_fact(conn, user, kind, statement)
    assert (refused.value.field, refused.value.issue) == (field, issue)
    assert state(conn, user) == before


def test_rows_of_other_people_do_not_exist_for_a_person(conn, user):
    other = make_user(conn, "bob")
    fact = unconfirmed_fact(conn, other)
    constraint = UUID(service.set_constraint(conn, other, *REMOTE).row["id"])
    preference = UUID(service.add_preference(conn, other, "industry", "Fintech", value="fintech", weight=1).row["id"])
    before = state(conn, user)
    for operation in (
        lambda: service.confirm_fact(conn, user, fact),
        lambda: service.correct_fact(conn, user, fact, "mine now"),
        lambda: service.reject_fact(conn, user, fact),
        lambda: service.change_constraint(conn, user, constraint, {"statement": "x"}),
        lambda: service.remove_constraint(conn, user, constraint),
        lambda: service.change_preference(conn, user, preference, {"weight": 1}),
        lambda: service.remove_preference(conn, user, preference),
        lambda: service.confirm_fact(conn, uuid4(), fact),  # a person who does not exist
        lambda: service.add_fact(conn, uuid4(), "skill", "Kubernetes"),
    ):
        with pytest.raises(RecordNotFound):
            operation()
    assert state(conn, user) == before and service.fact_row(conn, other, fact).status == "unconfirmed"


# ---- hard constraints ----------------------------------------------------------------------------


def test_setting_a_constraint_of_the_same_kind_replaces_the_active_one_and_keeps_the_old_row(conn, user):
    first = service.set_constraint(conn, user, "remote_policy", {"arrangements": ["remote"]}, "Remote only")
    second = service.set_constraint(
        conn, user, "remote_policy", {"arrangements": ["remote", "hybrid"]}, "Remote or hybrid"
    )

    rows = conn.execute(
        text("SELECT id, superseded_at IS NULL AS active, superseded_by FROM constraints WHERE user_id = :u"),
        {"u": user},
    ).all()
    assert len(rows) == 2 and [r.active for r in rows if str(r.id) == first.row["id"]] == [False]
    assert [str(r.superseded_by) for r in rows if str(r.id) == first.row["id"]] == [second.row["id"]]
    event = last_event(conn, user)
    assert event.before["arrangements"] == ["remote"] and event.after["arrangements"] == ["remote", "hybrid"]


def test_a_constraint_is_stored_as_typed_columns_and_shown_the_way_the_api_shows_it(conn, user):
    applied = service.set_constraint(
        conn, user, "salary_floor", {"salary_floor": {"amount": 3000000, "currency": "INR"}}, "Nothing below 30 lakh"
    )
    assert applied.row["salary_floor"] == {"amount": 3000000, "currency": "INR"}
    assert one(conn, "SELECT salary_floor FROM constraints WHERE user_id = :u", u=user) == 3000000
    assert one(conn, "SELECT salary_currency FROM constraints WHERE user_id = :u", u=user) == "INR"
    assert applied.row["source"] == "candidate_edit" and applied.row["arrangements"] is None
    listed = service.list_constraints(conn, user)
    assert [c["salary_floor"] for c in listed] == [{"amount": 3000000, "currency": "INR"}]


def test_every_kind_of_constraint_round_trips_through_the_database(conn, user):
    values = {
        "location": {"country_codes": ["IN", "US"], "cities": ["pune"]},
        "work_authorization": {"country_codes": ["IN"]},
        "remote_policy": {"arrangements": ["remote", "hybrid"]},
        "employment_type": {"employment_types": ["full_time", "contract"]},
        "seniority": {"seniorities": ["senior", "staff"]},
        "salary_floor": {"salary_floor": {"amount": 90000, "currency": "USD"}},
        "excluded_company": {"company_names": ["Globex Example Ltd"]},
    }
    for kind, fields in values.items():
        service.set_constraint(conn, user, kind, fields, f"A {kind} rule")
    stored = {c["kind"]: c for c in service.list_constraints(conn, user)}
    assert set(stored) == set(models.CONSTRAINT_KINDS)
    for kind, fields in values.items():
        assert {name: stored[kind][name] for name in fields} == fields
        assert all(stored[kind][name] is None for name in service.TYPED_FIELDS if name not in fields)


def test_the_values_of_a_constraint_are_normalized_not_guessed(conn, user):
    where = service.set_constraint(
        conn,
        user,
        "location",
        {"country_codes": ["IN", "IN", "US"], "cities": ["  Pune ", "PUNE", "New   Delhi"]},
        "Pune",
    )
    assert (where.row["country_codes"], where.row["cities"]) == (["IN", "US"], ["pune", "new delhi"])
    block = service.set_constraint(
        conn,
        user,
        "excluded_company",
        {"company_names": ["Globex Example Ltd", " globex example ltd", "Initech"]},
        "Not these",
    )
    assert block.row["company_names"] == ["Globex Example Ltd", "Initech"]  # as typed, the first spelling kept


@pytest.mark.parametrize(
    ("kind", "fields", "field", "issue"),
    [
        ("location", {}, "country_codes", "required"),
        ("location", {"country_codes": ["india"]}, "country_codes", "invalid_value"),
        ("location", {"country_codes": []}, "country_codes", "invalid_value"),
        ("location", {"country_codes": ["IN"], "cities": []}, "cities", "invalid_value"),
        ("remote_policy", {"arrangements": ["anywhere"]}, "arrangements", "invalid_value"),
        ("remote_policy", {"arrangements": "remote"}, "arrangements", "invalid_value"),
        ("remote_policy", {"arrangements": ["remote"], "country_codes": ["IN"]}, "country_codes", "not_used_by_kind"),
        ("remote_policy", {"arrangements": ["remote"], "bogus": 1}, "bogus", "unknown_field"),
        ("employment_type", {"employment_types": ["unknown"]}, "employment_types", "invalid_value"),
        ("seniority", {"seniorities": ["senior", "unknown"]}, "seniorities", "invalid_value"),
        ("salary_floor", {"salary_floor": 5}, "salary_floor", "invalid_value"),
        ("salary_floor", {"salary_floor": {"amount": 0, "currency": "INR"}}, "salary_floor.amount", "invalid_value"),
        ("salary_floor", {"salary_floor": {"amount": 10.5, "currency": "INR"}}, "salary_floor.amount", "invalid_value"),
        ("salary_floor", {"salary_floor": {"amount": True, "currency": "INR"}}, "salary_floor.amount", "invalid_value"),
        (
            "salary_floor",
            {"salary_floor": {"amount": 2**31, "currency": "INR"}},
            "salary_floor.amount",
            "invalid_value",
        ),
        (
            "salary_floor",
            {"salary_floor": {"amount": 100, "currency": "inr"}},
            "salary_floor.currency",
            "invalid_value",
        ),
        ("excluded_company", {"company_names": [""]}, "company_names", "required"),
        ("excluded_company", {"company_names": ["x" * 201]}, "company_names", "too_long"),
        ("excluded_company", {"company_names": ["A"] * 51}, "company_names", "invalid_value"),
    ],
)
def test_a_malformed_constraint_is_refused_before_the_database_and_writes_nothing(
    conn, user, kind, fields, field, issue
):
    before = state(conn, user)
    with pytest.raises(InvalidField) as refused:
        service.set_constraint(conn, user, kind, fields, "A rule")
    assert (refused.value.field, refused.value.issue) == (field, issue)
    assert state(conn, user) == before


def test_a_kind_the_matcher_cannot_enforce_is_refused_with_a_way_out(conn, user):
    before = state(conn, user)
    with pytest.raises(ConstraintNotEnforceable) as refused:
        service.set_constraint(conn, user, "industry_exclusion", {}, "No companies in gambling or betting.")
    assert refused.value.status == 422 and refused.value.code == "constraint_not_enforceable"
    assert refused.value.details["supported_kinds"] == list(models.CONSTRAINT_KINDS)
    assert refused.value.details["suggested_preference"] == {
        "kind": "untyped_dealbreaker",
        "statement": "No companies in gambling or betting.",
    }
    assert state(conn, user) == before


def test_a_constraint_is_never_sourced_from_a_resume():
    with pytest.raises(ValueError, match="never sourced from a resume"):
        Via("resume")
    with pytest.raises(ValueError, match="names its proposal"):
        Via("confirmed_proposal")
    with pytest.raises(ValueError):
        Via("candidate_edit", uuid4())


def test_changing_a_constraint_keeps_what_is_not_named_and_never_changes_its_kind(conn, user):
    where = service.set_constraint(conn, user, "location", {"country_codes": ["IN"], "cities": ["pune"]}, "Pune")
    changed = service.change_constraint(conn, user, UUID(where.row["id"]), {"country_codes": ["IN", "US"]})
    assert (changed.row["country_codes"], changed.row["cities"], changed.row["statement"]) == (
        ["IN", "US"],
        ["pune"],
        "Pune",
    )
    assert changed.row["id"] != where.row["id"]
    with pytest.raises(InvalidField) as refused:
        service.change_constraint(conn, user, UUID(changed.row["id"]), {"kind": "remote_policy"})
    assert refused.value.issue == "cannot_change"
    with pytest.raises(RecordNotFound):  # the replaced row is no longer the person's constraint
        service.change_constraint(conn, user, UUID(where.row["id"]), {"statement": "x"})
    with pytest.raises(InvalidField):  # a change that no longer fits the kind
        service.change_constraint(conn, user, UUID(changed.row["id"]), {"arrangements": ["remote"]})


def test_removing_a_constraint_retires_it_and_lets_another_of_that_kind_follow(conn, user):
    first = service.set_constraint(conn, user, *REMOTE)
    removed = service.remove_constraint(conn, user, UUID(first.row["id"]))
    assert removed.row["id"] == first.row["id"] and service.list_constraints(conn, user) == []
    with pytest.raises(RecordNotFound):
        service.remove_constraint(conn, user, UUID(first.row["id"]))
    service.set_constraint(conn, user, *REMOTE)
    assert last_event(conn, user).operation == "create"  # nothing active, so this is a new one, not an update


# ---- soft preferences and the dealbreaker (REC-04) -----------------------------------------------


def test_a_typed_preference_needs_its_value_and_a_weight(conn, user):
    added = service.add_preference(
        conn, user, "target_title", "I want a staff role", value=" Staff Engineer ", weight=2
    )
    assert (added.row["value"], added.row["weight"], added.row["notice"]) == ("Staff Engineer", 2, None)
    for kind, kwargs, field, issue in (
        ("target_title", {"weight": 2}, "value", "required"),
        ("target_title", {"value": "staff engineer"}, "weight", "required"),
        ("target_title", {"value": "staff engineer", "weight": 0}, "weight", "invalid_value"),
        ("target_title", {"value": "staff engineer", "weight": 4}, "weight", "invalid_value"),
        ("target_title", {"value": "staff engineer", "weight": True}, "weight", "invalid_value"),
        ("target_title", {"value": "x" * 201, "weight": 1}, "value", "too_long"),
        ("astronaut", {"value": "x", "weight": 1}, "kind", "invalid_value"),
    ):
        with pytest.raises(InvalidField) as refused:
            service.add_preference(conn, user, kind, "A statement", **kwargs)
        assert (refused.value.field, refused.value.issue) == (field, issue)
    assert service.list_preferences(conn, user)[0] == [added.row]  # the refusals wrote nothing


def test_a_preference_of_a_kind_with_no_vocabulary_takes_its_words_as_the_value(conn, user):
    added = service.add_preference(conn, user, "other", "  I like   small TEAMS ", weight=1)
    assert (added.row["statement"], added.row["value"]) == ("I like   small TEAMS", "i like small teams")


@pytest.mark.parametrize(
    ("statement", "kind", "fields"),
    [
        ("No remote work", "remote_policy", {"arrangements": ["onsite", "hybrid"]}),
        (
            "No contract work",
            "employment_type",
            {"employment_types": ["full_time", "part_time", "internship", "temporary"]},
        ),
        ("At least 30 LPA", "salary_floor", {"salary_floor": {"amount": 3000000, "currency": "INR"}}),
        ("not Globex Example Ltd", "excluded_company", {"company_names": ["Globex Example Ltd"]}),
    ],
)
def test_a_dealbreaker_that_fits_a_constraint_kind_is_saved_soft_and_offered_never_applied(
    conn, user, statement, kind, fields
):
    saved = service.add_preference(conn, user, "untyped_dealbreaker", statement)

    assert (saved.row["kind"], saved.row["weight"], saved.row["notice"]) == (
        "untyped_dealbreaker",
        -3,
        "not_guaranteed",
    )
    assert one(conn, "SELECT weight FROM preferences WHERE user_id = :u", u=user) == -3
    suggestion = saved.row["suggested_constraint"]
    assert {name: suggestion[name] for name in fields} == fields and suggestion["kind"] == kind
    assert suggestion["statement"] == statement
    assert one(conn, "SELECT count(*) FROM constraints WHERE user_id = :u", u=user) == 0  # nothing became a hard rule
    # the suggestion is exactly what POST /v1/me/constraints takes: confirming it works
    fields_only = {name: suggestion[name] for name in service.TYPED_FIELDS}
    confirmed = service.set_constraint(conn, user, suggestion["kind"], fields_only, suggestion["statement"])
    assert confirmed.row["kind"] == kind and confirmed.row["source"] == "candidate_edit"


def test_a_dealbreaker_no_job_field_can_check_is_saved_with_only_the_notice(conn, user):
    saved = service.add_preference(conn, user, "untyped_dealbreaker", "No companies in gambling or betting.")
    assert (saved.row["weight"], saved.row["notice"], saved.row["suggested_constraint"]) == (-3, "not_guaranteed", None)
    assert service.list_preferences(conn, user)[0][0]["notice"] == "not_guaranteed"
    assert "suggested_constraint" not in service.list_preferences(conn, user)[0][0]  # only the write offers it
    with pytest.raises(InvalidField) as refused:  # a dealbreaker is -3 or nothing
        service.add_preference(conn, user, "untyped_dealbreaker", "No gambling", weight=-1)
    assert refused.value.field == "weight"
    assert service.add_preference(conn, user, "untyped_dealbreaker", "No betting", weight=-3).row["weight"] == -3


def test_changing_a_dealbreakers_words_changes_its_value_with_them(conn, user):
    saved = service.add_preference(conn, user, "untyped_dealbreaker", "No gambling")
    changed = service.change_preference(conn, user, UUID(saved.row["id"]), {"statement": "No betting companies"})
    assert (changed.row["value"], changed.row["weight"]) == ("no betting companies", -3)
    typed = service.add_preference(conn, user, "target_title", "A staff role", value="staff engineer", weight=2)
    kept = service.change_preference(conn, user, UUID(typed.row["id"]), {"statement": "A senior staff role"})
    assert kept.row["value"] == "staff engineer"  # a typed preference keeps its token unless given a new one
    with pytest.raises(InvalidField) as refused:
        service.change_preference(conn, user, UUID(kept.row["id"]), {"kind": "industry"})
    assert refused.value.issue == "cannot_change"


def test_removing_and_changing_a_preference_retire_the_old_row(conn, user):
    first = service.add_preference(conn, user, "industry", "Fintech", value="fintech", weight=1)
    second = service.change_preference(conn, user, UUID(first.row["id"]), {"weight": 3})
    assert one(conn, "SELECT superseded_by FROM preferences WHERE id = :i", i=UUID(first.row["id"])) == UUID(
        second.row["id"]
    )
    assert [p["id"] for p in service.list_preferences(conn, user)[0]] == [second.row["id"]]
    service.remove_preference(conn, user, UUID(second.row["id"]))
    assert service.list_preferences(conn, user) == ([], None)
    with pytest.raises(RecordNotFound):
        service.remove_preference(conn, user, UUID(first.row["id"]))


# ---- reading the person's own record: paging, filters, header ------------------------------------


def test_facts_page_in_order_with_a_cursor_and_filter_by_status(conn, user):
    facts = [unconfirmed_fact(conn, user, start, start + 10) for start in (30, 0, 20, 10)]  # made in one transaction
    service.confirm_fact(conn, user, facts[1])
    service.reject_fact(conn, user, facts[3])

    everything, _ = service.list_facts(conn, user, limit=100)
    assert [f["provenance"]["span"]["start"] for f in everything] == [0, 10, 20, 30]  # document order, not insert order

    seen, cursor = [], None
    while True:
        batch, cursor = service.list_facts(conn, user, limit=3, cursor=cursor)
        seen += [f["id"] for f in batch]
        if cursor is None:
            break
    assert seen == [f["id"] for f in everything]

    by_status = {s: [f["id"] for f in service.list_facts(conn, user, status=s)[0]] for s in service._FACT_STATUS_WHERE}
    assert by_status == {
        "unconfirmed": [str(facts[2]), str(facts[0])],  # spans 20 and 30
        "confirmed": [str(facts[1])],
        "rejected": [str(facts[3])],
        "superseded": [],
    }
    with pytest.raises(InvalidField):
        service.list_facts(conn, user, status="pending")


def test_rows_written_in_one_transaction_list_in_the_order_they_were_written(conn, user):
    """now() is one value for a whole transaction, so created_at alone would leave their order to the random ids."""
    names = [f"Skill {n}" for n in range(8)]
    for name in names:
        service.add_fact(conn, user, "skill", name)
        service.add_preference(conn, user, "other", name, weight=1)
    assert [f["statement"] for f in service.list_facts(conn, user, limit=100)[0]] == names
    assert [p["statement"] for p in service.list_preferences(conn, user, limit=100)[0]] == names
    assert [e["after"]["statement"] for e in reversed(events.list_events(conn, user, limit=100)[0])][::2] == names


@pytest.mark.parametrize("cursor", ["", "nonsense", "e30", "WzEsMiwzXQ", "W10"])
def test_a_cursor_this_api_did_not_issue_is_refused(conn, user, cursor):
    for listing in (service.list_facts, service.list_preferences, events.list_events):
        with pytest.raises(BadCursor):
            listing(conn, user, cursor=cursor)


def test_the_header_counts_what_waits_for_the_candidate_and_names_the_version(conn, user):
    header = service.record_header(conn, user)
    assert (header.record_version, header.unconfirmed_facts, header.open_proposals) == (0, 0, 0)
    unconfirmed_fact(conn, user)
    unconfirmed_fact(conn, user, 5, 15)
    events.propose_change(
        conn,
        user,
        origin="chat",
        entity="constraint",
        operation="create",
        after={"kind": "remote_policy", "arrangements": ["remote"], "statement": "Remote"},
    )
    service.set_constraint(conn, user, *REMOTE)
    header = service.record_header(conn, user)
    assert (header.record_version, header.unconfirmed_facts, header.open_proposals) == (1, 2, 1)
    with pytest.raises(RecordNotFound):
        service.record_header(conn, uuid4())


def test_the_record_page_lists_uploads_without_their_content(conn, user):
    unconfirmed_fact(conn, user)
    (document,) = service.record_documents(conn, user)
    assert document["status"] == "succeeded" and "content" not in document and "extracted_text" not in document
    assert set(document) == {
        "id",
        "original_filename",
        "mime_type",
        "byte_size",
        "status",
        "parse_error_code",
        "hidden_text_chars",
        "created_at",
        "parsed_at",
    }
