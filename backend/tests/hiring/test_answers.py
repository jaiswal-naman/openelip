"""HIR-06: the employer answers an accepted introduction once, yes or no, with a reason (plan 06-04)."""

from sqlalchemy import text

from tests.hiring.test_introduction_routes import act, another_ask, key, rows, scene


def post(client, intro, body, headers=None):
    return client.post(f"/v1/introductions/{intro}/answer", json=body, headers=headers or key())


def test_a_no_with_a_reason_is_stored_and_the_candidate_is_told(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)

    response = post(api_client, s.ask.id, {"answer": "no", "reason": "  We need five years of team leadership.  "})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["introduction"]["employer_answer"] == "no"
    assert data["introduction"]["employer_answer_reason"] == "We need five years of team leadership."
    assert data["introduction"]["is_on_time"] is True and data["introduction"]["status"] == "accepted"
    assert data["candidate_sees"] == f"{s.org.name} said no: We need five years of team leadership."
    assert data["email"] is None  # only a yes releases it
    stored = rows(engine, "SELECT employer_answer::text, employer_answered_by, employer_answered_at FROM introductions")
    assert stored[0].employer_answer == "no" and stored[0].employer_answered_by == s.member
    assert stored[0].employer_answered_at is not None


def test_a_yes_returns_the_email_from_the_frozen_preview(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    data = post(api_client, s.ask.id, {"answer": "yes", "reason": "Strong fit"}).json()["data"]
    assert data["email"] == s.candidate.email == s.ask.preview["on_employer_yes"]["email"]
    assert data["candidate_sees"] == f"{s.org.name} said yes: Strong fit"


def test_the_answer_queues_one_email_to_the_candidate_and_one_audit_row_about_no_one(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    post(api_client, s.ask.id, {"answer": "yes", "reason": "Strong fit"})

    mail = rows(engine, "SELECT dedupe_key, user_id, payload FROM outbox WHERE template = 'introduction_answered_v1'")
    assert len(mail) == 1 and mail[0].user_id == s.candidate.id
    assert mail[0].dedupe_key == f"introduction_answered:{s.ask.id}"
    assert mail[0].payload == {"introduction_id": str(s.ask.id)}  # ids only, never the reason
    audit = rows(
        engine,
        "SELECT actor_type::text, actor_user_id, subject_user_id, organization_id, details FROM audit_log "
        "WHERE event_type = 'hiring.introduction_answered'",
    )
    assert len(audit) == 1 and (audit[0].actor_type, audit[0].actor_user_id) == ("employer", s.member)
    assert audit[0].subject_user_id is None and audit[0].organization_id == s.org.id
    assert "Strong fit" not in str(audit[0].details)


def test_a_blank_or_missing_or_too_long_reason_is_422_and_stores_nothing(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    for body in (
        {"answer": "yes", "reason": ""},
        {"answer": "yes", "reason": "   \n "},
        {"answer": "yes"},
        {"answer": "maybe", "reason": "x"},
        {"answer": "yes", "reason": "x" * 1001},
    ):
        refused = post(api_client, s.ask.id, body)
        assert refused.status_code == 422 and refused.json()["error"]["code"] == "validation_failed", body
    assert post(api_client, s.ask.id, {"answer": "yes", "reason": "x" * 1000}).status_code == 200
    assert rows(engine, "SELECT employer_answer::text FROM introductions") == [("yes",)]


def test_a_second_answer_is_already_resolved_with_the_first_one(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    post(api_client, s.ask.id, {"answer": "no", "reason": "Skills gap"})

    refused = post(api_client, s.ask.id, {"answer": "yes", "reason": "On second thought"})

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "already_resolved"
    assert refused.json()["error"]["details"] == {"resolution": "no"}
    assert rows(engine, "SELECT employer_answer::text, employer_answer_reason FROM introductions") == [
        ("no", "Skills gap")
    ]


def test_an_asked_introduction_is_404_to_the_employer_and_so_is_another_organizations(engine, api_client):
    s = scene(engine)
    _, asked = another_ask(engine, s)
    act(api_client, s.member)
    assert post(api_client, asked.id, {"answer": "yes", "reason": "x"}).status_code == 404
    assert post(api_client, s.ask.id, {"answer": "yes", "reason": "x"}).status_code == 404
    other = scene(engine)  # a second organization with its own member
    act(api_client, other.member)
    accepted = scene(engine, accepted=True)
    assert post(api_client, accepted.ask.id, {"answer": "yes", "reason": "x"}).status_code == 404
    assert rows(engine, "SELECT count(*) FROM introductions WHERE employer_answer IS NOT NULL") == [(0,)]


def test_a_candidate_cannot_answer_their_own_introduction(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)
    assert post(api_client, s.ask.id, {"answer": "yes", "reason": "x"}).status_code == 404
    assert rows(engine, "SELECT employer_answer FROM introductions") == [(None,)]


def test_an_overdue_answer_is_late_but_still_accepted(engine, api_client):
    s = scene(engine, accepted=True)
    with engine.begin() as c:
        c.execute(text("SET LOCAL app.maintenance = 'on'"))
        c.execute(text("UPDATE introductions SET answer_due_at = now() - interval '1 day'"))
    act(api_client, s.member)
    data = post(api_client, s.ask.id, {"answer": "no", "reason": "Role filled"}).json()["data"]
    assert data["introduction"]["is_on_time"] is False
