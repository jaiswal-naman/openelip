"""MAT-08: save, unsave and dismiss a match with a reason, and a dismissal that can only propose a record change (plan 04-04).

Two kinds of test. The logic of record_feedback and of the reason-to-proposal mapping runs on the rolled-back `conn`
fixture, as the record's service tests do. The route (validation, the Idempotency-Key, ownership and the whole
dismiss-then-confirm path through /v1/me/events) runs through api_client as the real app_api role, on rows it commits.
Everything is invented: the candidate, "Initech Demo", the postings.
"""

import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.match import feedback, ledger
from app.platform.errors import ApiError
from app.platform.sessions import Principal, current_user
from app.profile import api as profile
from app.profile import service
from tests.match.factories import Verdict, make_decision, make_job, make_record, make_run, make_shown_decision, make_user


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


def send(client, decision, body, key: str | None = None):
    return client.post(f"/v1/matches/{decision}/feedback", json=body, headers={"Idempotency-Key": key or uuid4().hex})


def match(conn, *, company="Initech Demo", title="Data engineer", city=None, country=None, seniority="unknown"):
    """A candidate with a confirmed record and one shown match on a job with the given stated fields."""
    user = make_user(conn)
    make_record(conn, user)
    job = make_job(
        conn,
        title=title,
        description="Python.",
        requirements=[("Python.", "required")],
        company_name=company,
        city=city,
        country=country,
        seniority=seniority,
    )
    decision = make_shown_decision(conn, make_run(conn, user), user, job, [Verdict("no_evidence")], rank_position=1)
    return SimpleNamespace(user=user, job=job, decision=decision)


def proposal(conn, event_id: UUID):
    return (
        conn.execute(
            text(
                """SELECT event_type::text AS event_type, entity::text AS entity, operation::text AS operation,
                          entity_id, after, origin::text AS origin, origin_ref, reason
                   FROM profile_events WHERE id = :id"""
            ),
            {"id": event_id},
        )
        .mappings()
        .one()
    )


def violations(conn, user: UUID, job) -> list[str]:
    return conn.execute(
        text("SELECT CAST(constraint_violations(j, compile_constraints(:u)) AS text[]) FROM jobs j WHERE j.id = :job"),
        {"u": user, "job": job.id},
    ).scalar_one()


def events(conn, user: UUID) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        text("SELECT kind::text, dismiss_reason::text FROM feedback_events WHERE user_id = :u ORDER BY created_at, id"),
        {"u": user},
    )
    return [(r[0], r[1]) for r in rows]


def record_state(conn, user: UUID) -> tuple:
    version = conn.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": user}).scalar_one()
    return version, profile.compiled_constraints(conn, user), len(profile.get_record(conn, user)["preferences"])


# ---- record_feedback -----------------------------------------------------------------------------------------------


def test_a_save_is_kept_and_an_unsave_after_it_leaves_the_job_unsaved_with_both_events_on_record(conn):
    m = match(conn)
    assert feedback.record_feedback(conn, m.user, m.decision, "save", None, None) == feedback.FeedbackResult(
        True, False, None
    )
    assert feedback.record_feedback(conn, m.user, m.decision, "unsave", None, None) == feedback.FeedbackResult(
        False, False, None
    )
    assert events(conn, m.user) == [("save", None), ("unsave", None)]
    row = conn.execute(
        text("SELECT job_id, match_decision_id FROM feedback_events WHERE user_id = :u LIMIT 1"), {"u": m.user}
    ).one()
    assert (row.job_id, row.match_decision_id) == (m.job.id, m.decision)  # the job is copied from the decision


def test_a_dismissal_is_the_latest_state_and_a_save_after_it_takes_it_back(conn):
    m = match(conn)
    assert feedback.record_feedback(conn, m.user, m.decision, "dismiss", "other", "Not for me").is_dismissed is True
    assert ledger.feedback_state(conn, m.user, [m.job.id]) == {m.job.id: (False, True)}
    assert feedback.record_feedback(conn, m.user, m.decision, "save", None, None) == feedback.FeedbackResult(
        True, False, None
    )
    assert conn.execute(text("SELECT comment FROM feedback_events WHERE kind = 'dismiss'")).scalar_one() == "Not for me"


def test_feedback_on_a_job_with_no_events_is_neither_saved_nor_dismissed(conn):
    m = match(conn)
    assert ledger.feedback_state(conn, m.user, [m.job.id]) == {}


def withheld(conn, user: UUID) -> UUID:
    """A decision that is not a shown match: a job the person's run held back."""
    job = make_job(conn, title="Held", description="Go.", requirements=[("Go.", "required")])
    return make_decision(conn, make_run(conn, user, trigger="on_demand"), user, job)


def test_feedback_on_another_persons_decision_or_on_a_decision_that_is_not_a_shown_match_is_a_404_and_writes_nothing(conn):
    ada, bob = match(conn), match(conn)
    for decision in (bob.decision, withheld(conn, ada.user), uuid4()):
        with pytest.raises(ApiError) as refused_:
            feedback.record_feedback(conn, ada.user, decision, "save", None, None)
        assert (refused_.value.status, refused_.value.code) == (404, "not_found")
    assert conn.execute(text("SELECT count(*) FROM feedback_events")).scalar_one() == 0


def test_a_dismissal_of_a_company_proposes_excluding_it_and_changes_nothing_until_the_person_confirms(conn):
    m = match(conn)
    before = record_state(conn, m.user)
    result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", "not_interested_company", None)

    assert result.is_dismissed and result.proposal_event_id is not None
    row = proposal(conn, result.proposal_event_id)
    assert (row["event_type"], row["entity"], row["operation"], row["entity_id"]) == (
        "proposed",
        "constraint",
        "create",
        None,
    )
    assert row["after"]["kind"] == "excluded_company" and row["after"]["company_names"] == ["Initech Demo"]
    assert row["origin"] == "feedback"
    event = conn.execute(text("SELECT id FROM feedback_events WHERE user_id = :u"), {"u": m.user}).scalar_one()
    assert row["origin_ref"] == str(event)  # the proposal says which click it came from
    assert row["reason"] == "You dismissed a job at Initech Demo as a company you are not interested in."

    assert record_state(conn, m.user) == before  # nothing changed: not the version, not a constraint
    assert violations(conn, m.user, m.job) == []

    profile.confirm(conn, m.user, result.proposal_event_id)
    assert violations(conn, m.user, m.job) == ["excluded_company"]  # now the job is filtered by the person's own rule


def test_a_company_added_to_an_existing_exclusion_keeps_the_names_already_there(conn):
    m = match(conn)
    existing = service.set_constraint(
        conn, m.user, "excluded_company", {"company_names": ["Ashgrove Capital"]}, "Never Ashgrove Capital."
    ).row["id"]
    result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", "not_interested_company", None)

    row = proposal(conn, result.proposal_event_id)
    assert (row["operation"], str(row["entity_id"])) == ("update", existing)  # the one active row is replaced, so ...
    assert row["after"]["company_names"] == ["Ashgrove Capital", "Initech Demo"]  # ... it must carry both names
    assert row["after"]["statement"] == "Never Ashgrove Capital, Initech Demo."
    profile.confirm(conn, m.user, result.proposal_event_id)
    assert profile.compiled_constraints(conn, m.user)["excluded_company"]["company_names"] == [
        "Ashgrove Capital",
        "Initech Demo",
    ]


@pytest.mark.parametrize("typed", ["Initech Demo", "INITECH DEMO, LLC", "initech-demo inc."])
def test_a_company_that_is_already_excluded_under_any_spelling_proposes_nothing(conn, typed):
    m = match(conn)
    service.set_constraint(conn, m.user, "excluded_company", {"company_names": [typed]}, "Never them.")
    assert feedback.record_feedback(conn, m.user, m.decision, "dismiss", "not_interested_company", None).proposal_event_id is None


def test_a_dismissal_of_a_role_proposes_a_negative_target_title_preference(conn):
    m = match(conn, title="Senior   Data Engineer")
    result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", "not_interested_role", None)
    row = proposal(conn, result.proposal_event_id)
    assert (row["entity"], row["operation"], row["origin"]) == ("preference", "create", "feedback")
    assert (row["after"]["kind"], row["after"]["value"], row["after"]["weight"]) == ("target_title", "senior data engineer", -2)
    assert row["reason"] == "You dismissed Senior Data Engineer at Initech Demo as a role you are not interested in."
    profile.confirm(conn, m.user, result.proposal_event_id)
    (preference,) = profile.get_record(conn, m.user)["preferences"]
    assert (preference["kind"], preference["value"], preference["weight"]) == ("target_title", "senior data engineer", -2)


def test_a_seniority_dismissal_proposes_only_when_the_posting_states_its_seniority(conn):
    stated, silent = match(conn, seniority="senior"), match(conn)
    row = proposal(
        conn, feedback.record_feedback(conn, stated.user, stated.decision, "dismiss", "seniority_mismatch", None).proposal_event_id
    )
    assert (row["after"]["kind"], row["after"]["value"], row["after"]["weight"]) == ("seniority", "senior", -2)
    unknown = feedback.record_feedback(conn, silent.user, silent.decision, "dismiss", "seniority_mismatch", None)
    assert unknown.proposal_event_id is None and unknown.is_dismissed


def test_a_location_dismissal_proposes_the_stated_city_else_the_stated_country_else_nothing(conn):
    city, country, neither = match(conn, city="Bengaluru", country="IN"), match(conn, country="DE"), match(conn)
    values = []
    for m in (city, country):
        result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", "location", None)
        row = proposal(conn, result.proposal_event_id)
        values.append((row["after"]["kind"], row["after"]["value"], row["after"]["weight"]))
    assert values == [("location", "bengaluru", -2), ("location", "de", -2)]
    assert feedback.record_feedback(conn, neither.user, neither.decision, "dismiss", "location", None).proposal_event_id is None


@pytest.mark.parametrize(
    "reason",
    ["skills_mismatch", "compensation", "already_applied", "duplicate", "job_closed", "other", "explanation_wrong"],
)
def test_every_other_reason_is_recorded_and_proposes_nothing(conn, reason):
    m = match(conn, city="Bengaluru", country="IN", seniority="senior")
    before = record_state(conn, m.user)
    result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", reason, None)
    assert (result.is_dismissed, result.proposal_event_id) == (True, None)
    assert events(conn, m.user) == [("dismiss", reason)]
    assert conn.execute(text("SELECT count(*) FROM profile_events WHERE event_type = 'proposed'")).scalar_one() == 0
    assert record_state(conn, m.user) == before


def test_a_report_that_the_explanation_is_wrong_is_logged_with_ids_only_for_the_weekly_audit(conn, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    m = match(conn)
    feedback.record_feedback(conn, m.user, m.decision, "dismiss", "explanation_wrong", "It says I know Go.")
    (line,) = [r.getMessage() for r in caplog.records if "match.explanation_reported" in r.getMessage()]
    assert str(m.decision) in line and str(m.job.id) in line
    assert "It says I know Go" not in line and "Initech" not in line  # no words of anyone's


def test_a_proposal_the_record_refuses_does_not_stop_the_dismissal_from_being_kept(conn, caplog):
    """A company name the record's own screen refuses (it states a protected characteristic) makes no proposal, and the
    person's click is still recorded."""
    caplog.set_level(logging.INFO, logger="app.event")
    m = match(conn, company="Disability Rights Fund")
    before = record_state(conn, m.user)
    result = feedback.record_feedback(conn, m.user, m.decision, "dismiss", "not_interested_company", None)
    assert (result.is_dismissed, result.proposal_event_id) == (True, None)
    assert events(conn, m.user) == [("dismiss", "not_interested_company")]
    assert conn.execute(text("SELECT count(*) FROM profile_events WHERE event_type = 'proposed'")).scalar_one() == 0
    assert record_state(conn, m.user) == before
    assert "Disability" not in caplog.text  # the refused words are not logged either


# ---- POST /v1/matches/{decision_id}/feedback ----------------------------------------------------------------------


@pytest.fixture
def world(api_client, engine) -> SimpleNamespace:
    with engine.begin() as conn:
        w = match(conn)
        w.bob = match(conn)
        w.held = withheld(conn, w.user)
    w.client = api_client
    w.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    w.sign_in(w.user)
    return w


def stored(engine, user: UUID) -> list[tuple[str, str | None]]:
    with engine.connect() as conn:
        return events(conn, user)


def test_save_then_unsave_through_the_route_leaves_it_unsaved_and_both_rows_remain(world, engine):
    first = ok(send(world.client, world.decision, {"kind": "save"}), 201)
    assert first == {"is_saved": True, "is_dismissed": False, "proposal_event_id": None}
    second = ok(send(world.client, world.decision, {"kind": "unsave"}), 201)
    assert second == {"is_saved": False, "is_dismissed": False, "proposal_event_id": None}
    assert stored(engine, world.user) == [("save", None), ("unsave", None)]


def test_a_repeat_with_the_same_key_returns_the_first_response_and_inserts_nothing(world, engine):
    key, body = uuid4().hex, {"kind": "save", "comment": "Look again on Friday."}
    first = send(world.client, world.decision, body, key)
    again = send(world.client, world.decision, body, key)
    assert again.status_code == 201 and again.json()["data"] == first.json()["data"]
    assert again.json()["meta"]["idempotent_replay"] is True
    assert stored(engine, world.user) == [("save", None)]
    refused(send(world.client, world.decision, {"kind": "unsave"}, key), 422, "idempotency_key_reused")


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "dismiss"},  # a dismissal needs its reason
        {"kind": "save", "reason": "other"},  # and nothing else has one
        {"kind": "dismiss", "reason": "bored"},
        {"kind": "dismiss", "reason": None},
        {"kind": "star"},
        {"kind": "save", "comment": "x" * 501},
        {"kind": "save", "comment": "bad\x00text"},
        {},
    ],
)
def test_a_body_that_does_not_fit_is_422_and_stores_nothing(world, engine, body):
    refused(send(world.client, world.decision, body), 422, "validation_failed")
    assert stored(engine, world.user) == []


def test_a_comment_of_exactly_500_characters_is_kept(world, engine):
    assert ok(send(world.client, world.decision, {"kind": "dismiss", "reason": "other", "comment": "x" * 500}), 201)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT length(comment) FROM feedback_events")).scalar_one() == 500


def test_a_write_without_a_key_or_not_as_json_gets_the_phase_1_errors(world):
    path = f"/v1/matches/{world.decision}/feedback"
    refused(world.client.post(path, json={"kind": "save"}), 400, "idempotency_key_missing")
    form = world.client.post(path, data={"kind": "save"}, headers={"Idempotency-Key": uuid4().hex})
    assert (form.status_code, form.json()["error"]["code"]) == (415, "unsupported_media_type")


def test_it_needs_a_signed_in_person(api_client):
    refused(send(api_client, uuid4(), {"kind": "save"}), 401, "unauthenticated")


def test_feedback_on_someone_elses_decision_a_withheld_one_or_an_unknown_one_is_404_and_inserts_nothing(world, engine):
    for decision in (world.bob.decision, world.held, uuid4()):
        refused(send(world.client, decision, {"kind": "save"}), 404, "not_found")
    assert stored(engine, world.user) == [] and stored(engine, world.bob.user) == []


def test_dismissing_a_company_through_the_route_then_confirming_the_proposal_filters_the_job(world, engine):
    """The whole path: the click records a proposal and nothing else, and only the person's own confirmation (the record
    route that already exists) changes the record."""
    answer = ok(send(world.client, world.decision, {"kind": "dismiss", "reason": "not_interested_company"}), 201)
    assert (answer["is_saved"], answer["is_dismissed"]) == (False, True)
    event = answer["proposal_event_id"]
    assert event is not None

    waiting = ok(world.client.get("/v1/me/events", params={"status": "proposed"}))
    assert [(e["id"], e["origin"], e["entity"]) for e in waiting] == [(event, "feedback", "constraint")]
    assert ok(world.client.get("/v1/me/record"))["record_version"] == 1  # unchanged
    with engine.connect() as conn:
        assert violations(conn, world.user, world.job) == []

    confirmed = world.client.post(f"/v1/me/events/{event}/confirm", headers={"Idempotency-Key": uuid4().hex})
    assert ok(confirmed)["entity"] == "constraint"
    with engine.connect() as conn:
        assert violations(conn, world.user, world.job) == ["excluded_company"]
