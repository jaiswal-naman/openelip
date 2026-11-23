"""HIR-06: GET /v1/orgs/{id}/answer-clock reports what is owed, due times in the organization's time zone, overdue state
and whether new asks are paused (plan 06-04)."""

from datetime import datetime, timezone

from sqlalchemy import text

from app.hiring.answers import business_days_between
from tests.hiring.test_introduction_routes import act, another_ask, key, make_stranger, rows, scene

KOLKATA = "Asia/Kolkata"


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_business_days_left_counts_monday_to_friday_in_the_organizations_zone():
    friday = utc(2026, 11, 20, 9, 0)
    monday_due = utc(2026, 11, 23, 9, 0)
    assert business_days_between(friday, monday_due, "UTC") == 1  # a Monday seen from the Friday before
    assert business_days_between(utc(2026, 11, 23, 9, 0), utc(2026, 11, 30, 9, 0), "UTC") == 5  # five business days
    assert business_days_between(monday_due, monday_due, "UTC") == 0
    assert business_days_between(monday_due, utc(2026, 11, 22, 9, 0), "UTC") == 0  # overdue
    assert business_days_between(utc(2026, 11, 21, 9, 0), utc(2026, 11, 22, 9, 0), "UTC") == 0  # a weekend away
    # 22:00 UTC on a Sunday is already Monday in Kolkata, so the next day of that week is Tuesday there
    assert business_days_between(utc(2026, 11, 22, 22, 0), utc(2026, 11, 24, 9, 0), KOLKATA) == 1
    assert business_days_between(utc(2026, 11, 22, 22, 0), utc(2026, 11, 24, 9, 0), "UTC") == 2


def make_overdue(engine, intro, hours=1) -> None:
    with engine.begin() as c:
        c.execute(text("SET LOCAL app.maintenance = 'on'"))
        c.execute(
            text("UPDATE introductions SET answer_due_at = now() - make_interval(hours => :h) WHERE id = :i"),
            {"h": hours, "i": intro},
        )


def test_an_organization_with_nothing_owed_is_not_paused(engine, api_client):
    s = scene(engine)
    act(api_client, s.member)

    clock = api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").json()["data"]

    assert clock["org_id"] == str(s.org.id) and clock["timezone"] == "UTC" and clock["as_of"]
    assert clock["is_asking_paused"] is False and clock["paused_since"] is None and clock["pause_reason"] is None
    assert clock["owed"] == [] and clock["answered_last_30_days"] == {"total": 0, "on_time": 0}


def test_owed_answers_are_listed_by_due_time_with_the_name_and_days_left(engine, api_client):
    s = scene(engine, accepted=True, timezone_name=KOLKATA)
    second, ask = another_ask(engine, s, accepted=True)
    asked_candidate, _ = another_ask(engine, s, name="Demo Only Asked")  # only asked: not owed, not named
    act(api_client, s.member)

    clock = api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").json()["data"]

    assert clock["timezone"] == KOLKATA and clock["is_asking_paused"] is False
    assert [o["introduction_id"] for o in clock["owed"]] == [str(s.ask.id), str(ask.id)]
    first = clock["owed"][0]
    assert first["name"] == s.ask.preview["on_accept"]["name"] and first["role"] == {
        "id": str(s.role.id),
        "title": s.role.title,
    }
    assert first["is_overdue"] is False and 4 <= first["business_days_left"] <= 5
    assert first["responded_at"] and first["answer_due_at"]
    assert asked_candidate.name not in str(clock)


def test_an_overdue_answer_pauses_asking_and_names_since_when(engine, api_client):
    s = scene(engine, accepted=True)
    make_overdue(engine, s.ask.id)
    act(api_client, s.member)

    clock = api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").json()["data"]

    assert clock["is_asking_paused"] is True and clock["paused_since"] == clock["owed"][0]["answer_due_at"]
    assert clock["owed"][0]["is_overdue"] is True and clock["owed"][0]["business_days_left"] == 0
    assert "1 answer is overdue" in clock["pause_reason"] and "resume" in clock["pause_reason"]
    assert rows(engine, "SELECT organization_has_overdue_answers(:o)", o=s.org.id) == [(True,)]


def test_answering_resumes_asking_and_counts_in_the_last_30_days(engine, api_client):
    s = scene(engine, accepted=True)
    make_overdue(engine, s.ask.id)
    act(api_client, s.member)
    api_client.post(
        f"/v1/introductions/{s.ask.id}/answer", json={"answer": "no", "reason": "Late, sorry"}, headers=key()
    )

    clock = api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").json()["data"]

    assert clock["is_asking_paused"] is False and clock["owed"] == []
    assert clock["answered_last_30_days"] == {"total": 1, "on_time": 0}  # answered after the due time
    second, ask = another_ask(engine, s, accepted=True)
    api_client.post(f"/v1/introductions/{ask.id}/answer", json={"answer": "yes", "reason": "Fit"}, headers=key())
    clock = api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").json()["data"]
    assert clock["answered_last_30_days"] == {"total": 2, "on_time": 1}


def test_another_organization_and_a_stranger_get_404(engine, api_client):
    s = scene(engine, accepted=True)
    other = scene(engine)
    act(api_client, other.member)
    assert api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").status_code == 404
    act(api_client, make_stranger(engine))
    assert api_client.get(f"/v1/orgs/{s.org.id}/answer-clock").status_code == 404
