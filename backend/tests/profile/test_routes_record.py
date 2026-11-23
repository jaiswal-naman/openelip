"""The Career Record routes under /v1/me, the profile export and the wiring into the two composition roots (plan 03-04).

The API opens its own connections as the real app_api role, so row-level security and the grants are in force and what a
request commits stays: these tests use api_client (which empties the tables around each test) and read what was stored
through the bootstrap engine. The person is made here and signed in by overriding the session dependency, as the other
route tests do; the sign-in flow has its own tests.
"""

import ast
import inspect
import json
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from app.platform import config, db, queue
from app.platform.sessions import Principal, current_user
from app.profile import events, jobs, routes, service
from app.profile import privacy as profile_privacy
from app.trust import privacy as trust_privacy

TEXT = "Senior Backend Engineer, Northwind Logistics (Mar 2021 - Present)\nBuilt order pipelines in Python."
RECORD_TABLES = ("documents", "profile_events", "profile_facts", "constraints", "preferences")
SALARY = {
    "kind": "salary_floor",
    "salary_floor": {"amount": 3000000, "currency": "INR"},
    "statement": "Nothing below 30 lakh rupees a year in base pay.",
}
REMOTE = {"kind": "remote_policy", "arrangements": ["remote"], "statement": "Remote only"}


def make_person(engine, email: str = "ada@example.test") -> UUID:
    with engine.begin() as c:
        return c.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


@pytest.fixture
def person(api_client, engine):
    """The API client acting as a signed-in person. api_client.user is their id."""
    api_client.user = make_person(engine)
    api_client.app.dependency_overrides[current_user] = lambda: Principal(api_client.user)
    return api_client


def seed_unconfirmed(engine, user: UUID, spans=((0, 10), (11, 20), (21, 40))) -> list[str]:
    """Facts extracted from a resume and waiting for review, as plan 03-05's parse job will write them."""
    with engine.begin() as c:
        document = c.execute(
            text(
                """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                          extracted_text, hidden_text_chars, parsed_at)
                   VALUES (:u, :c, 'resume.pdf', 'application/pdf', 100, :h, 'parsed', :t, 7, now()) RETURNING id"""
            ),
            {"u": user, "c": b"sealed", "h": b"h" * 32, "t": TEXT},
        ).scalar_one()
        return [
            str(
                c.execute(
                    text(
                        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span,
                                                      source_quote)
                           VALUES (:u, 'role', :q, 'resume', :d, int4range(:a, :b), :q) RETURNING id"""
                    ),
                    {"u": user, "d": document, "a": a, "b": b, "q": TEXT[a:b]},
                ).scalar_one()
            )
            for a, b in spans
        ]


def send(client, method: str, path: str, body=None, key: str | None = None):
    return client.request(method, path, json=body, headers={"Idempotency-Key": key or uuid4().hex})


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def fails(client, method: str, path: str, body=None, status: int = 422, code: str = "validation_failed") -> dict:
    """Send a write and expect the error envelope with this status and code; returns its error object."""
    return refused(send(client, method, path, body), status, code)


def create(client, path: str, body) -> dict:
    """POST a new row and expect 201."""
    return ok(send(client, "POST", path, body), 201)


def walk(client, path: str, **params) -> tuple[list[dict], int]:
    """Every row of a list, page by page by the cursor in meta.next_cursor, and how many pages that took."""
    rows, pages, cursor = [], 0, None
    while True:
        response = client.get(path, params={**params, **({"cursor": cursor} if cursor else {})})
        rows += ok(response)
        pages += 1
        assert response.json()["meta"]["limit"] == params["limit"]
        cursor = response.json()["meta"]["next_cursor"]
        if cursor is None:
            return rows, pages


def pick(row: dict, *keys: str) -> tuple:
    return tuple(row[key] for key in keys)


def scalar(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def counts(engine, user: UUID) -> dict:
    tables = {t: scalar(engine, f"SELECT count(*) FROM {t} WHERE user_id = :u", u=user) for t in RECORD_TABLES}
    return {**tables, "version": scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=user)}


# ---- the record ----------------------------------------------------------------------------------


def test_an_empty_record_is_one_object_and_needs_no_consent(person, engine):
    data = ok(person.get("/v1/me/record"))
    assert data == {
        "record_version": 0,
        "facts": [],
        "constraints": [],
        "preferences": [],
        "unconfirmed_facts": 0,
        "open_proposals": 0,
        "documents": [],
    }
    routes.RecordView.model_validate(data)
    # no consent was ever given, and the record still reads, takes a fact by hand and takes a constraint
    assert scalar(engine, "SELECT count(*) FROM consents WHERE user_id = :u", u=person.user) == 0
    assert send(person, "POST", "/v1/me/facts", {"kind": "skill", "statement": "Kubernetes"}).status_code == 201
    assert send(person, "POST", "/v1/me/constraints", REMOTE).status_code == 201


def test_the_record_page_shows_what_is_confirmed_and_only_counts_the_rest(person, engine):
    waiting = seed_unconfirmed(engine, person.user)
    create(person, "/v1/me/facts", {"kind": "skill", "statement": "Kubernetes"})
    create(person, "/v1/me/constraints", SALARY)
    create(person, "/v1/me/preferences", {"kind": "industry", "statement": "Fintech", "value": "fintech", "weight": 1})
    with db.transaction(person.user) as conn:
        events.propose_change(
            conn,
            person.user,
            origin="chat",
            entity="fact",
            operation="create",
            after={"kind": "skill", "statement": "Go"},
        )

    record = ok(person.get("/v1/me/record"))

    routes.RecordView.model_validate(record)
    assert [f["statement"] for f in record["facts"]] == ["Kubernetes"]  # the three waiting facts are not listed
    assert (record["unconfirmed_facts"], record["open_proposals"], record["record_version"]) == (len(waiting), 1, 3)
    assert [c["kind"] for c in record["constraints"]] == ["salary_floor"] and len(record["preferences"]) == 1
    (document,) = record["documents"]
    assert pick(document, "original_filename", "status", "hidden_text_chars") == ("resume.pdf", "succeeded", 7)
    assert "content" not in document and "extracted_text" not in document


# ---- facts ---------------------------------------------------------------------------------------


def test_a_fact_written_by_hand_is_confirmed_on_creation(person, engine):
    data = create(person, "/v1/me/facts", {"kind": "skill", "statement": "  Kubernetes "})
    routes.FactWritten.model_validate(data)
    assert pick(data["fact"], "status", "source", "provenance") == ("confirmed", "candidate_edit", None)
    assert (data["fact"]["statement"], data["record_version"]) == ("Kubernetes", 1)
    assert data["fact"]["created_at"].endswith("Z")  # the same RFC 3339 form the reads use (API.md 1.4)
    assert [f["id"] for f in ok(person.get("/v1/me/record"))["facts"]] == [data["fact"]["id"]]


def test_facts_page_with_a_cursor_and_filter_by_status(person, engine):
    seed_unconfirmed(engine, person.user, spans=((30, 40), (0, 10), (20, 30), (10, 20), (40, 50)))
    create(person, "/v1/me/facts", {"kind": "skill", "statement": "Kubernetes"})

    seen, pages = walk(person, "/v1/me/facts", status="unconfirmed", limit=2)
    assert pages == 3 and [f["provenance"]["span"]["start"] for f in seen] == [0, 10, 20, 30, 40]  # document order
    assert all(f["status"] == "unconfirmed" for f in seen) and len({f["id"] for f in seen}) == 5
    routes.RecordFact.model_validate(seen[0])
    assert seen[0]["provenance"] == {
        "document_id": seen[0]["provenance"]["document_id"],
        "span": {"start": 0, "end": 10},
        "quote": TEXT[0:10],
    }
    assert len(ok(person.get("/v1/me/facts", params={"status": "confirmed"}))) == 1
    assert len(ok(person.get("/v1/me/facts", params={"limit": 100}))) == 6  # every status when no filter is given

    assert refused(person.get("/v1/me/facts", params={"cursor": "nonsense"}), 400, "invalid_request")
    for params in ({"limit": 0}, {"limit": 101}, {"status": "pending"}):
        refused(person.get("/v1/me/facts", params=params), 422, "validation_failed")


def test_a_review_confirms_corrects_and_rejects_in_one_write(person, engine):
    confirm, correct, reject = seed_unconfirmed(engine, person.user)
    body = {
        "decisions": [
            {"fact_id": confirm, "action": "confirm"},
            {"fact_id": correct, "action": "correct", "statement": "Led the migration of the order pipeline"},
            {"fact_id": reject, "action": "reject", "reason": "Only used it in a course"},
        ]
    }

    data = ok(send(person, "POST", "/v1/me/facts/review", body))

    routes.FactsReviewed.model_validate(data)
    assert (data["record_version"], data["unconfirmed_remaining"]) == (3, 0)
    first, second, third = data["results"]
    assert (first["fact_id"], first["status"]) == (confirm, "confirmed")
    assert (second["fact_id"], second["status"]) == (correct, "superseded")
    assert second["replaced_by"]["status"] == "confirmed" and second["replaced_by"]["source"] == "candidate_edit"
    assert (third["fact_id"], third["status"]) == (reject, "rejected")
    record = ok(person.get("/v1/me/record"))
    assert sorted(f["statement"] for f in record["facts"]) == sorted(
        [TEXT[0:10], "Led the migration of the order pipeline"]
    )
    log = ok(person.get("/v1/me/events"))
    assert [e["operation"] for e in log] == ["reject", "update", "confirm"] and log[0][
        "reason"
    ] == "Only used it in a course"


def test_a_review_is_all_or_nothing_and_cannot_name_another_persons_fact(person, engine):
    (mine, *_) = seed_unconfirmed(engine, person.user)
    other = make_person(engine, "bob@example.test")
    (theirs, *_) = seed_unconfirmed(engine, other)
    before = counts(engine, person.user)

    both = {"decisions": [{"fact_id": mine, "action": "confirm"}, {"fact_id": theirs, "action": "confirm"}]}
    fails(person, "POST", "/v1/me/facts/review", both, 404, "not_found")

    assert counts(engine, person.user) == before  # the first decision was undone with the second
    assert scalar(engine, "SELECT confirmed_at IS NULL FROM profile_facts WHERE id = :f", f=UUID(theirs))
    twice = {"decisions": [{"fact_id": mine, "action": "confirm"}, {"fact_id": mine, "action": "reject"}]}
    error = fails(person, "POST", "/v1/me/facts/review", twice)
    assert error["details"] == {"fields": [{"field": "decisions", "issue": "duplicate"}]}
    for bad in (
        {"decisions": []},
        {"decisions": [{"fact_id": mine, "action": "correct"}]},  # a correction carries the candidate's words
        {"decisions": [{"fact_id": mine, "action": "delete"}]},
        {"decisions": [{"fact_id": "not-a-uuid", "action": "confirm"}]},
    ):
        fails(person, "POST", "/v1/me/facts/review", bad)
    assert counts(engine, person.user) == before


def test_a_fact_in_the_wrong_state_is_a_conflict_that_names_the_state(person, engine):
    (fact, *_) = seed_unconfirmed(engine, person.user)
    ok(send(person, "POST", "/v1/me/facts/review", {"decisions": [{"fact_id": fact, "action": "reject"}]}))
    again = {"decisions": [{"fact_id": fact, "action": "confirm"}]}
    error = fails(person, "POST", "/v1/me/facts/review", again, 409, "conflict")
    assert error["details"] == {"state": "rejected"}


# ---- hard constraints ----------------------------------------------------------------------------


def test_a_constraint_is_added_changed_and_removed_with_every_change_logged(person, engine):
    created = create(person, "/v1/me/constraints", SALARY)
    routes.ConstraintWritten.model_validate(created)
    assert created["constraint"]["salary_floor"] == {"amount": 3000000, "currency": "INR"}
    assert (created["constraint"]["source"], created["record_version"]) == ("candidate_edit", 1)
    assert created["constraint"]["arrangements"] is None  # a kind uses only its own fields

    raise_floor = {"salary_floor": {"amount": 3500000, "currency": "INR"}}
    changed = ok(send(person, "PATCH", f"/v1/me/constraints/{created['constraint']['id']}", raise_floor))
    assert changed["constraint"]["id"] != created["constraint"]["id"] and changed["record_version"] == 2
    assert changed["constraint"]["statement"] == SALARY["statement"]  # what is not named stays
    assert [c["id"] for c in ok(person.get("/v1/me/constraints"))] == [changed["constraint"]["id"]]

    removed = ok(send(person, "DELETE", f"/v1/me/constraints/{changed['constraint']['id']}"))
    assert removed == {"record_version": 3} and ok(person.get("/v1/me/constraints")) == []
    log = ok(person.get("/v1/me/events"))
    assert [(e["operation"], e["record_version_after"]) for e in log] == [("remove", 3), ("update", 2), ("create", 1)]
    assert (
        log[1]["before"]["salary_floor"]["amount"] == 3000000 and log[1]["after"]["salary_floor"]["amount"] == 3500000
    )


def test_one_constraint_per_kind_so_adding_another_replaces_the_first(person):
    first = create(person, "/v1/me/constraints", REMOTE)["constraint"]
    second = create(person, "/v1/me/constraints", {**REMOTE, "arrangements": ["remote", "hybrid"]})["constraint"]
    assert [c["id"] for c in ok(person.get("/v1/me/constraints"))] == [second["id"]] and first["id"] != second["id"]


def test_a_kind_the_matcher_cannot_enforce_is_422_with_a_preference_to_save_instead(person, engine):
    body = {"kind": "industry_exclusion", "statement": "No companies in gambling or betting."}
    error = refused(send(person, "POST", "/v1/me/constraints", body), 422, "constraint_not_enforceable")
    assert error["details"]["supported_kinds"] == [
        "location",
        "remote_policy",
        "work_authorization",
        "salary_floor",
        "employment_type",
        "seniority",
        "excluded_company",
    ]
    assert error["details"]["suggested_preference"] == {"kind": "untyped_dealbreaker", "statement": body["statement"]}
    assert counts(engine, person.user)["constraints"] == 0
    saved = create(person, "/v1/me/preferences", error["details"]["suggested_preference"])  # the client's next step
    assert (saved["preference"]["weight"], saved["preference"]["notice"]) == (-3, "not_guaranteed")


@pytest.mark.parametrize(
    ("body", "field"),
    [
        (
            {"kind": "remote_policy", "statement": "x", "arrangements": ["anywhere"]},
            "arrangements.0",
        ),  # the enum, from the schema
        ({"kind": "location", "statement": "x", "country_codes": ["india"]}, "country_codes"),
        ({"kind": "location", "statement": "x"}, "country_codes"),
        ({"kind": "remote_policy", "statement": "x", "arrangements": ["remote"], "cities": ["pune"]}, "cities"),
        (
            {"kind": "salary_floor", "statement": "x", "salary_floor": {"amount": 0, "currency": "INR"}},
            "salary_floor.amount",
        ),
        (
            {"kind": "salary_floor", "statement": "x", "salary_floor": {"amount": 10, "currency": "rupees"}},
            "salary_floor.currency",
        ),
        ({"kind": "excluded_company", "statement": "x", "company_names": [""]}, "company_names"),
        ({"kind": "remote_policy", "statement": ""}, "statement"),
        ({"kind": "remote_policy", "arrangements": ["remote"]}, "statement"),
    ],
)
def test_a_malformed_constraint_is_a_field_error_and_stores_nothing(person, engine, body, field):
    error = fails(person, "POST", "/v1/me/constraints", body)
    assert field in [f["field"] for f in error["details"]["fields"]]
    assert counts(engine, person.user)["constraints"] == 0


def test_a_constraints_kind_never_changes(person):
    created = create(person, "/v1/me/constraints", REMOTE)["constraint"]
    error = fails(person, "PATCH", f"/v1/me/constraints/{created['id']}", {"kind": "location"})
    assert error["details"]["fields"] == [{"field": "kind", "issue": "cannot_change"}]


# ---- soft preferences ----------------------------------------------------------------------------


def test_a_dealbreaker_that_fits_a_constraint_is_saved_soft_and_the_suggestion_is_postable(person, engine):
    data = create(person, "/v1/me/preferences", {"kind": "untyped_dealbreaker", "statement": "No remote work"})

    routes.PreferenceWritten.model_validate(data)
    saved = data["preference"]
    assert (saved["kind"], saved["weight"], saved["notice"]) == ("untyped_dealbreaker", -3, "not_guaranteed")
    suggestion = saved["suggested_constraint"]
    assert (suggestion["kind"], suggestion["arrangements"], suggestion["statement"]) == (
        "remote_policy",
        ["onsite", "hybrid"],
        "No remote work",
    )
    assert (
        ok(person.get("/v1/me/constraints")) == [] and counts(engine, person.user)["constraints"] == 0
    )  # offered, not applied

    confirmed = create(person, "/v1/me/constraints", suggestion)  # the candidate says yes
    assert confirmed["constraint"]["kind"] == "remote_policy" and confirmed["constraint"]["source"] == "candidate_edit"


def test_a_dealbreaker_no_job_field_can_check_carries_only_the_notice(person):
    body = {"kind": "untyped_dealbreaker", "statement": "No companies in gambling or betting."}
    saved = create(person, "/v1/me/preferences", body)["preference"]
    assert (saved["weight"], saved["notice"], saved["suggested_constraint"]) == (-3, "not_guaranteed", None)
    (listed,) = ok(person.get("/v1/me/preferences"))
    assert listed["notice"] == "not_guaranteed" and listed["suggested_constraint"] is None
    fails(
        person, "POST", "/v1/me/preferences", {"kind": "untyped_dealbreaker", "statement": "No gambling", "weight": 2}
    )


def test_preferences_are_added_changed_removed_and_paged(person):
    ids = []
    for n in range(5):
        body = {"kind": "target_skill", "statement": f"Skill {n}", "value": f"skill {n}", "weight": 1}
        ids.append(create(person, "/v1/me/preferences", body)["preference"]["id"])
    seen, pages = walk(person, "/v1/me/preferences", limit=2)
    assert [p["id"] for p in seen] == ids and pages == 3

    changed = ok(send(person, "PATCH", f"/v1/me/preferences/{ids[0]}", {"weight": 3}))
    assert (changed["preference"]["weight"], changed["preference"]["value"], changed["record_version"]) == (
        3,
        "skill 0",
        6,
    )
    removed = ok(send(person, "DELETE", f"/v1/me/preferences/{ids[1]}"))
    assert removed == {"record_version": 7}
    now = [p["id"] for p in ok(person.get("/v1/me/preferences", params={"limit": 100}))]
    assert ids[0] not in now and ids[1] not in now and len(now) == 4
    for body in (
        {"kind": "industry", "statement": "x", "weight": 1},
        {"kind": "industry", "statement": "x", "value": "y"},
        {"kind": "industry", "statement": "x", "value": "y", "weight": 0},
    ):
        fails(person, "POST", "/v1/me/preferences", body)


# ---- one person never reaches another's rows (T-3-12) --------------------------------------------


def test_another_persons_rows_are_404_and_nothing_of_theirs_is_listed(person, engine):
    other = make_person(engine, "bob@example.test")
    (theirs, *_) = seed_unconfirmed(engine, other)
    with db.transaction(other) as conn:
        constraint = service.set_constraint(conn, other, "remote_policy", {"arrangements": ["remote"]}, "Remote").row[
            "id"
        ]
        preference = service.add_preference(conn, other, "industry", "Fintech", value="fintech", weight=1).row["id"]
        proposal = events.propose_change(
            conn, other, origin="chat", entity="fact", operation="create", after={"kind": "skill", "statement": "Go"}
        )
    before = counts(engine, other)

    for method, path, body in (
        ("PATCH", f"/v1/me/constraints/{constraint}", {"statement": "mine now"}),
        ("DELETE", f"/v1/me/constraints/{constraint}", None),
        ("PATCH", f"/v1/me/preferences/{preference}", {"weight": 1}),
        ("DELETE", f"/v1/me/preferences/{preference}", None),
        ("POST", f"/v1/me/events/{proposal}/confirm", None),
        ("POST", f"/v1/me/events/{proposal}/decline", None),
        ("POST", "/v1/me/facts/review", {"decisions": [{"fact_id": theirs, "action": "reject"}]}),
    ):
        fails(person, method, path, body, 404, "not_found")
    assert counts(engine, other) == before
    record = ok(person.get("/v1/me/record"))
    assert (record["facts"], record["constraints"], record["preferences"], record["documents"]) == ([], [], [], [])
    assert ok(person.get("/v1/me/facts")) == [] and ok(person.get("/v1/me/events", params={"status": "proposed"})) == []


def test_the_person_is_the_session_never_the_body(person, engine):
    other = make_person(engine, "bob@example.test")
    body = {"kind": "skill", "statement": "Kubernetes", "user_id": str(other)}
    create(person, "/v1/me/facts", body)
    assert (counts(engine, person.user)["profile_facts"], counts(engine, other)["profile_facts"]) == (1, 0)


# ---- REC-07 over HTTP ----------------------------------------------------------------------------


WRITES = {
    "fact": (lambda s: ("POST", "/v1/me/facts", lambda t: {"kind": "skill", "statement": t}), "statement"),
    "constraint statement": (
        lambda s: ("POST", "/v1/me/constraints", lambda t: {**REMOTE, "statement": t}),
        "statement",
    ),
    "constraint company": (
        lambda s: (
            "POST",
            "/v1/me/constraints",
            lambda t: {"kind": "excluded_company", "company_names": [t], "statement": "Not these"},
        ),
        "company_names",
    ),
    "constraint city": (
        lambda s: (
            "POST",
            "/v1/me/constraints",
            lambda t: {"kind": "location", "country_codes": ["IN"], "cities": [t], "statement": "Somewhere"},
        ),
        "cities",
    ),
    "preference statement": (
        lambda s: ("POST", "/v1/me/preferences", lambda t: {"kind": "other", "weight": 1, "statement": t}),
        "statement",
    ),
    "preference value": (
        lambda s: (
            "POST",
            "/v1/me/preferences",
            lambda t: {"kind": "industry", "weight": 1, "value": t, "statement": "Fintech"},
        ),
        "value",
    ),
    "dealbreaker": (
        lambda s: ("POST", "/v1/me/preferences", lambda t: {"kind": "untyped_dealbreaker", "statement": t}),
        "statement",
    ),
    "review correction": (
        lambda s: (
            "POST",
            "/v1/me/facts/review",
            lambda t: {"decisions": [{"fact_id": s["fact"], "action": "correct", "statement": t}]},
        ),
        "statement",
    ),
    "review reason": (
        lambda s: (
            "POST",
            "/v1/me/facts/review",
            lambda t: {"decisions": [{"fact_id": s["fact"], "action": "reject", "reason": t}]},
        ),
        "reason",
    ),
    "constraint change": (
        lambda s: ("PATCH", f"/v1/me/constraints/{s['constraint']}", lambda t: {"statement": t}),
        "statement",
    ),
    "preference change": (
        lambda s: ("PATCH", f"/v1/me/preferences/{s['preference']}", lambda t: {"value": t}),
        "value",
    ),
}


@pytest.mark.parametrize("sentence", ["Religion: Jainism", "Date of birth: 04/03/1998"])
@pytest.mark.parametrize("name", sorted(WRITES))
def test_a_protected_characteristic_is_a_422_that_names_the_field_and_stores_nothing(person, engine, name, sentence):
    (fact, *_) = seed_unconfirmed(engine, person.user)
    india = {"kind": "location", "country_codes": ["IN"], "statement": "India"}
    constraint = create(person, "/v1/me/constraints", india)["constraint"]["id"]
    fintech = {"kind": "industry", "statement": "Fintech", "value": "fintech", "weight": 1}
    preference = create(person, "/v1/me/preferences", fintech)["preference"]["id"]
    ids = {"fact": fact, "constraint": constraint, "preference": preference}
    before = counts(engine, person.user)
    build, field = WRITES[name]
    method, path, body = build(ids)

    response = send(person, method, path, body(sentence))

    error = refused(response, 422, "validation_failed")
    assert error["details"] == {"fields": [{"field": field, "issue": "protected_characteristic"}]}
    assert sentence not in response.text and "Jainism" not in response.text and "04/03/1998" not in response.text
    assert counts(engine, person.user) == before  # no row of the record, no version, no event


# ---- the change log and proposals ----------------------------------------------------------------


def test_the_log_lists_changes_newest_first_with_a_cursor(person):
    for n in range(5):
        create(person, "/v1/me/facts", {"kind": "skill", "statement": f"Skill {n}"})
    seen, _ = walk(person, "/v1/me/events", limit=2)
    assert [e["record_version_after"] for e in seen] == [5, 4, 3, 2, 1]
    routes.RecordEvent.model_validate(seen[0])
    assert pick(seen[0], "status", "source", "operation", "before") == ("applied", "candidate_edit", "create", None)
    assert seen[0]["after"]["statement"] == "Skill 4" and seen[0]["created_at"]
    assert refused(person.get("/v1/me/events", params={"status": "pending"}), 422, "validation_failed")


def test_a_proposal_waits_for_the_candidate_and_is_answered_once(person, engine):
    with db.transaction(person.user) as conn:
        proposal = events.propose_change(
            conn,
            person.user,
            origin="chat",
            entity="constraint",
            operation="create",
            after=REMOTE,
            reason="You said remote",
        )
        declined = events.propose_change(
            conn,
            person.user,
            origin="feedback",
            entity="fact",
            operation="create",
            after={"kind": "skill", "statement": "Go"},
        )
    waiting = ok(person.get("/v1/me/events", params={"status": "proposed"}))
    assert {e["id"] for e in waiting} == {str(proposal), str(declined)} and {e["origin"] for e in waiting} == {
        "chat",
        "feedback",
    }
    assert ok(person.get("/v1/me/events")) == [] and counts(engine, person.user)["constraints"] == 0

    confirmed = ok(send(person, "POST", f"/v1/me/events/{proposal}/confirm"))
    routes.ProposalConfirmed.model_validate(confirmed)
    assert (confirmed["entity"], confirmed["record_version"]) == ("constraint", 1)
    assert [c["source"] for c in ok(person.get("/v1/me/constraints"))] == ["confirmed_proposal"]
    error = fails(person, "POST", f"/v1/me/events/{proposal}/confirm", None, 409, "already_resolved")
    assert error["details"] == {"resolution": "applied"}

    no = ok(send(person, "POST", f"/v1/me/events/{declined}/decline"))
    routes.ProposalDeclined.model_validate(no)
    error = fails(person, "POST", f"/v1/me/events/{declined}/confirm", None, 409, "already_resolved")
    assert error["details"] == {"resolution": "declined"}
    assert ok(person.get("/v1/me/events", params={"status": "proposed"})) == []
    log = ok(person.get("/v1/me/events"))
    assert [(e["status"], e["origin"]) for e in log] == [("declined", "feedback"), ("applied", "chat")]
    assert ok(person.get("/v1/me/record"))["open_proposals"] == 0 and counts(engine, person.user)["profile_facts"] == 0


# ---- the Phase 1 envelope, session and idempotency rules apply -----------------------------------


ID = uuid4()
ROUTES = [
    ("GET", "/v1/me/record"),
    ("GET", "/v1/me/facts"),
    ("POST", "/v1/me/facts"),
    ("POST", "/v1/me/facts/review"),
    ("GET", "/v1/me/constraints"),
    ("POST", "/v1/me/constraints"),
    ("PATCH", f"/v1/me/constraints/{ID}"),
    ("DELETE", f"/v1/me/constraints/{ID}"),
    ("GET", "/v1/me/preferences"),
    ("POST", "/v1/me/preferences"),
    ("PATCH", f"/v1/me/preferences/{ID}"),
    ("DELETE", f"/v1/me/preferences/{ID}"),
    ("GET", "/v1/me/events"),
    ("POST", f"/v1/me/events/{ID}/confirm"),
    ("POST", f"/v1/me/events/{ID}/decline"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_record_route_needs_a_signed_in_person(api_client, method, path):
    fails(api_client, method, path, {} if method in ("POST", "PATCH") else None, 401, "unauthenticated")


@pytest.mark.parametrize(("method", "path"), [r for r in ROUTES if r[0] != "GET"])
def test_every_record_write_needs_an_idempotency_key_and_json(person, method, path):
    assert person.request(method, path, json={}).json()["error"]["code"] == "idempotency_key_missing"
    form = person.request(method, path, data={"a": "b"}, headers={"Idempotency-Key": uuid4().hex})
    assert (form.status_code, form.json()["error"]["code"]) == (415, "unsupported_media_type")


def test_a_repeated_write_replays_the_first_answer_and_changes_nothing(person, engine):
    key, body = uuid4().hex, {"kind": "skill", "statement": "Kubernetes"}
    first = send(person, "POST", "/v1/me/facts", body, key)
    again = send(person, "POST", "/v1/me/facts", body, key)

    assert again.status_code == 201 and again.json()["data"] == first.json()["data"]
    assert again.json()["meta"]["idempotent_replay"] is True and "idempotent_replay" not in first.json()["meta"]
    assert counts(engine, person.user)["profile_facts"] == 1
    reused = send(person, "POST", "/v1/me/facts", {**body, "statement": "Helm"}, key)
    assert refused(reused, 422, "idempotency_key_reused")


def test_the_openapi_document_describes_the_record_and_marks_untrusted_text(api_client):
    spec = api_client.get("/openapi.json").json()
    paths = spec["paths"]
    assert {m for m in paths["/v1/me/constraints/{constraint_id}"]} == {"patch", "delete"}
    assert {m for m in paths["/v1/me/events/{event_id}/confirm"]} == {"post"} and "get" in paths["/v1/me/record"]
    schemas = spec["components"]["schemas"]
    assert schemas["FactProvenance"]["properties"]["quote"]["x-untrusted-text"] is True
    assert schemas["RecordFact"]["properties"]["statement"]["x-untrusted-text"] is True
    assert set(schemas["RecordView"]["properties"]) == {
        "record_version",
        "facts",
        "constraints",
        "preferences",
        "unconfirmed_facts",
        "open_proposals",
        "documents",
    }


# ---- registration, export and the two composition roots ------------------------------------------


def test_the_profile_tables_are_registered_for_deletion_and_export():
    assert trust_privacy.registry()["profile"] == dict.fromkeys(RECORD_TABLES, "cascade")
    assert trust_privacy.exporters()["profile"] is profile_privacy.export_profile


def test_the_worker_registers_the_profile_handlers_and_the_registry_in_its_own_process():
    import app.worker as worker

    assert worker.profile_jobs is jobs and worker.profile_privacy is profile_privacy
    calls = [n for n in ast.walk(ast.parse(inspect.getsource(worker.main))) if isinstance(n, ast.Call)]
    assert any(ast.unparse(call.func) == "profile_jobs.register" for call in calls)
    before = dict(queue._handlers)
    jobs.register()
    assert queue._handlers == before  # none yet: plan 03-05 adds profile.parse


def test_the_api_mounts_the_profile_router_and_imports_the_registration():
    import app.main as main

    assert main.profile_routes.router is routes.router and main.profile_privacy is profile_privacy
    assert "/v1/me/record" in main.create_app().openapi()["paths"]


def seal(data: bytes) -> bytes:
    return Fernet(config.get().data_keys[0]).encrypt(data)


def test_the_export_holds_every_row_of_every_status_and_each_original_file(conn):
    user = conn.execute(text("INSERT INTO users (email) VALUES ('ada@example.test') RETURNING id")).scalar_one()
    stranger = conn.execute(text("INSERT INTO users (email) VALUES ('bob@example.test') RETURNING id")).scalar_one()
    pdf, docx = b"%PDF-1.4 an invented resume", b"PK\x03\x04 an invented docx"
    for who, name, content, mime in (
        (user, "../../etc/Asha Example CV.pdf", pdf, "application/pdf"),
        (user, None, docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        (stranger, "theirs.pdf", b"%PDF-1.4 their secret", "application/pdf"),
    ):
        conn.execute(
            text(
                """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                          extracted_text, parsed_at)
                   VALUES (:u, :c, :n, :m, :s, :h, 'parsed', :t, now())"""
            ),
            {"u": who, "c": seal(content), "n": name, "m": mime, "s": len(content), "h": b"h" * 32, "t": TEXT},
        )
    document = conn.execute(
        text("SELECT id FROM documents WHERE user_id = :u ORDER BY created_at, id LIMIT 1"), {"u": user}
    ).scalar_one()
    kept = conn.execute(
        text(
            """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
               VALUES (:u, 'role', :q, 'resume', :d, int4range(0, 25), :q) RETURNING id"""
        ),
        {"u": user, "d": document, "q": TEXT[0:25]},
    ).scalar_one()
    gone = conn.execute(
        text(
            """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
               VALUES (:u, 'role', :q, 'resume', :d, int4range(26, 40), :q) RETURNING id"""
        ),
        {"u": user, "d": document, "q": TEXT[26:40]},
    ).scalar_one()
    service.confirm_fact(conn, user, kept)
    service.reject_fact(conn, user, gone)
    service.correct_fact(conn, user, kept, "Led the order pipeline")
    service.set_constraint(conn, user, "remote_policy", {"arrangements": ["remote"]}, "Remote only")
    service.set_constraint(conn, user, "remote_policy", {"arrangements": ["hybrid"]}, "Hybrid")
    service.add_preference(conn, user, "untyped_dealbreaker", "No gambling")
    service.add_fact(conn, stranger, "skill", "Their skill")

    export = profile_privacy.export_profile(conn, user)

    assert set(export.data) == set(RECORD_TABLES)
    json.dumps(export.data)  # JSON-ready as it stands
    assert {f["status"] for f in export.data["profile_facts"]} == {"superseded", "rejected", "confirmed"}
    assert all("source_quote" in f for f in export.data["profile_facts"])
    assert len(export.data["constraints"]) == 2 and len(export.data["preferences"]) == 1
    assert [e["operation"] for e in export.data["profile_events"]] == [
        "confirm",
        "reject",
        "update",
        "create",
        "update",
        "create",
    ]
    assert all(
        set(d) >= {"original_filename", "extracted_text", "sha256", "status"} and "content" not in d
        for d in export.data["documents"]
    )
    assert all(len(d["sha256"]) == 64 for d in export.data["documents"])
    assert "Their skill" not in json.dumps(export.data) and "theirs" not in json.dumps(export.data)
    paths = sorted(export.files)
    assert len(paths) == 2 and all(p.startswith("files/documents/") for p in paths)
    assert not any(".." in p or p.count("/") != 2 for p in paths)  # a name from the person cannot name a path
    assert sorted(export.files.values()) == sorted([pdf, docx])  # the originals, decrypted
    assert any(p.endswith("-etc_Asha_Example_CV.pdf") for p in paths) and any(p.endswith("-resume.docx") for p in paths)


@pytest.mark.parametrize("role", ["app_worker", "app_api"])
def test_the_export_works_under_the_role_that_runs_it(conn, role):
    user = conn.execute(text("INSERT INTO users (email) VALUES ('ada@example.test') RETURNING id")).scalar_one()
    conn.execute(
        text(
            """INSERT INTO documents (user_id, content, mime_type, byte_size, sha256, status)
               VALUES (:u, :c, 'application/pdf', 3, :h, 'uploaded')"""
        ),
        {"u": user, "c": seal(b"abc"), "h": b"h" * 32},
    )
    conn.execute(text(f"SET LOCAL ROLE {role}"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user) if role == "app_api" else ""})
    export = profile_privacy.export_profile(conn, user)
    assert list(export.files.values()) == [b"abc"] and len(export.data["documents"]) == 1
