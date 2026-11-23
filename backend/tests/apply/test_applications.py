"""APP-01: the application tracker, GET and POST /v1/applications and POST /v1/applications/{id}/status (plan 05-01).

The API opens its own connections as the real app_api role, so row-level security and the grants are in force and what a
test commits stays: these tests use api_client (it empties the tables around each test), write their rows with the
Phase 5 factories on a committing connection, and sign a person in by overriding the session dependency, as the match
routes' tests do. Everything here is invented.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.apply import api as apply_api
from app.platform.sessions import Principal, current_user
from tests.p5_factories import make_application, make_job, make_packet, make_user

UTC = timezone.utc


def ok(response, status: int = 200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body


def refused(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def when(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def count(engine, source: str, **params) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT count(*) FROM {source}"), params).scalar_one()


@pytest.fixture
def world(api_client, engine) -> SimpleNamespace:
    """Ada and Bob, two jobs, and nothing tracked yet. Ada is signed in."""
    w = SimpleNamespace()
    with engine.begin() as conn:
        w.ada, w.bob = make_user(conn), make_user(conn)
        w.job = make_job(
            conn, title="Data engineer", description="Build pipelines.", requirements=[("Build pipelines", "required")]
        )
        w.other = make_job(
            conn,
            title="Analyst",
            company_name="Hooli Demo",
            description="Analyse data.",
            requirements=[("Analyse data", "required")],
        )
    w.client = api_client
    w.engine = engine
    w.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    w.sign_in(w.ada)
    return w


def post(world, path: str, body: dict, *, key: str | None = None):
    return world.client.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})


def apply_for(world, job=None, **body):
    return post(world, "/v1/applications", {"job_id": (job or world.job).id, **body})


def move(world, application: str, status: str, occurred_at: datetime, *, key: str | None = None):
    return post(
        world,
        f"/v1/applications/{application}/status",
        {"status": status, "occurred_at": occurred_at.isoformat()},
        key=key,
    )


# ---- recording that the person applied ---------------------------------------------------------------------------


def test_recording_an_application_gives_applied_with_one_event_at_the_time_the_person_gave(world):
    given = datetime(2026, 11, 20, 9, 30, tzinfo=UTC)
    data = ok(apply_for(world, occurred_at=given.isoformat()), 201)["data"]

    assert (data["status"], data["job_id"], data["application_packet_id"]) == ("applied", world.job.id, None)
    assert (data["job_title"], data["company_name"]) == ("Data engineer", "Initech Demo")
    assert when(data["status_changed_at"]) == given
    (event,) = data["events"]
    assert (event["status"], when(event["occurred_at"])) == ("applied", given)
    assert abs(when(event["created_at"]) - datetime.now(UTC)) < timedelta(
        minutes=1
    )  # when it was recorded, not claimed
    assert count(world.engine, "applications WHERE user_id = :u", u=world.ada) == 1


def test_without_a_time_the_application_happened_now(world):
    data = ok(apply_for(world), 201)["data"]
    assert abs(when(data["status_changed_at"]) - datetime.now(UTC)) < timedelta(minutes=1)
    assert when(data["events"][0]["occurred_at"]) == when(data["status_changed_at"])


def test_the_same_key_gets_the_same_answer_and_records_nothing_twice(world):
    key = uuid4().hex
    first = ok(post(world, "/v1/applications", {"job_id": world.job.id}, key=key), 201)
    again = ok(post(world, "/v1/applications", {"job_id": world.job.id}, key=key), 201)
    assert again["data"] == first["data"] and again["meta"]["idempotent_replay"] is True
    assert count(world.engine, "applications") == 1 and count(world.engine, "application_events") == 1


def test_a_second_application_for_the_same_job_is_a_conflict_that_names_its_state(world):
    ok(apply_for(world), 201)
    error = refused(apply_for(world), 409, "conflict")
    assert error["details"] == {"state": "application_exists"}
    assert count(world.engine, "applications") == 1 and count(world.engine, "application_events") == 1


def test_another_person_may_apply_to_the_same_job_and_one_person_to_another_job(world):
    ok(apply_for(world), 201)
    ok(apply_for(world, job=world.other), 201)
    world.sign_in(world.bob)
    ok(apply_for(world), 201)
    assert count(world.engine, "applications") == 3


def test_an_unknown_job_is_not_found_and_nothing_is_recorded(world):
    refused(post(world, "/v1/applications", {"job_id": world.other.id + 1000}), 404, "not_found")
    assert count(world.engine, "applications") == 0


@pytest.mark.parametrize("job_id", [0, -1, 2**63, "seven", None], ids=["zero", "negative", "too-big", "text", "null"])
def test_a_job_id_that_cannot_be_one_is_refused_before_anything_is_read(world, job_id):
    refused(post(world, "/v1/applications", {"job_id": job_id}), 422, "validation_failed")


@pytest.mark.parametrize(
    "when_text",
    ["2026-11-20T09:30:00", "yesterday", 5],
    ids=["no-zone", "not-a-time", "a-number"],
)
def test_a_time_without_a_zone_or_that_is_not_a_time_is_refused(world, when_text):
    refused(apply_for(world, occurred_at=when_text), 422, "validation_failed")


def test_a_time_in_the_future_is_refused_but_a_little_clock_drift_is_not(world):
    later = datetime.now(UTC) + timedelta(minutes=10)
    error = refused(apply_for(world, occurred_at=later.isoformat()), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "occurred_at"
    assert count(world.engine, "applications") == 0
    ok(apply_for(world, occurred_at=(datetime.now(UTC) + timedelta(minutes=4)).isoformat()), 201)


@pytest.mark.parametrize("ancient", ["0001-01-01T00:00:00Z", "1999-12-31T23:59:59Z"])
def test_a_time_before_the_year_2000_is_refused_so_the_tracker_can_always_be_read_back(world, ancient):
    error = refused(apply_for(world, occurred_at=ancient), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "occurred_at"
    assert count(world.engine, "applications") == 0
    ok(apply_for(world, occurred_at="2000-01-01T00:00:00Z"), 201)  # the floor itself is allowed
    assert len(ok(world.client.get("/v1/applications"))["data"]) == 1


# ---- linking a packet ---------------------------------------------------------------------------------------------


def test_an_application_can_name_the_packet_that_was_made_for_the_job(world):
    with world.engine.begin() as conn:
        packet = make_packet(conn, world.ada, world.job)
    data = ok(apply_for(world, application_packet_id=str(packet)), 201)["data"]
    assert data["application_packet_id"] == str(packet)


def test_a_packet_of_another_person_or_of_another_job_or_that_is_not_there_is_not_found(world):
    with world.engine.begin() as conn:
        bobs = make_packet(conn, world.bob, world.job)
        for_other_job = make_packet(conn, world.ada, world.other)
    for packet in (bobs, for_other_job, uuid4()):
        refused(apply_for(world, application_packet_id=str(packet)), 404, "not_found")
    assert count(world.engine, "applications") == 0


# ---- moving on: interview, offer, rejected, no response ---------------------------------------------------------


def test_each_new_status_adds_an_event_and_moves_the_application(world):
    start = datetime.now(UTC) - timedelta(days=10)
    application = ok(apply_for(world, occurred_at=start.isoformat()), 201)["data"]["id"]
    interview, offer = start + timedelta(days=4), start + timedelta(days=9)
    data = ok(move(world, application, "interview", interview), 201)["data"]
    assert (data["status"], when(data["status_changed_at"])) == ("interview", interview)
    data = ok(move(world, application, "offer", offer), 201)["data"]
    assert (data["status"], when(data["status_changed_at"])) == ("offer", offer)
    assert [(e["status"], when(e["occurred_at"])) for e in data["events"]] == [
        ("applied", start),
        ("interview", interview),
        ("offer", offer),
    ]
    assert count(world.engine, "application_events WHERE application_id = :a", a=application) == 3


@pytest.mark.parametrize("status", ["interview", "offer", "rejected", "no_response"])
def test_every_status_of_the_tracker_can_be_recorded_after_applying(world, status):
    start = datetime.now(UTC) - timedelta(days=3)
    application = ok(apply_for(world, occurred_at=start.isoformat()), 201)["data"]["id"]
    data = ok(move(world, application, status, start + timedelta(days=1)), 201)["data"]
    assert data["status"] == status


def test_the_same_status_as_the_current_one_is_a_conflict_and_changes_nothing(world):
    start = datetime.now(UTC) - timedelta(days=3)
    application = ok(apply_for(world, occurred_at=start.isoformat()), 201)["data"]["id"]
    error = refused(move(world, application, "applied", start + timedelta(days=1)), 409, "conflict")
    assert error["details"] == {"state": "same_status"}
    assert count(world.engine, "application_events WHERE application_id = :a", a=application) == 1


def test_a_status_cannot_predate_the_current_one_or_lie_in_the_future(world):
    start = datetime.now(UTC) - timedelta(days=3)
    application = ok(apply_for(world, occurred_at=start.isoformat()), 201)["data"]["id"]
    for bad in (start - timedelta(minutes=1), datetime.now(UTC) + timedelta(minutes=10)):
        error = refused(move(world, application, "interview", bad), 422, "validation_failed")
        assert error["details"]["fields"][0]["field"] == "occurred_at"
    assert count(world.engine, "application_events WHERE application_id = :a", a=application) == 1
    ok(move(world, application, "interview", start), 201)  # the same moment as the current status is not before it


def test_a_status_needs_its_time_and_a_status_of_the_five(world):
    application = ok(apply_for(world), 201)["data"]["id"]
    path = f"/v1/applications/{application}/status"
    refused(post(world, path, {"status": "interview"}), 422, "validation_failed")
    refused(
        post(world, path, {"status": "ghosted", "occurred_at": datetime.now(UTC).isoformat()}), 422, "validation_failed"
    )
    refused(post(world, path, {"occurred_at": datetime.now(UTC).isoformat()}), 422, "validation_failed")


def test_the_same_key_moves_an_application_once(world):
    start = datetime.now(UTC) - timedelta(days=3)
    application = ok(apply_for(world, occurred_at=start.isoformat()), 201)["data"]["id"]
    key = uuid4().hex
    first = ok(move(world, application, "interview", start + timedelta(days=1), key=key), 201)
    again = ok(move(world, application, "interview", start + timedelta(days=1), key=key), 201)
    assert again["data"] == first["data"] and again["meta"]["idempotent_replay"] is True
    assert count(world.engine, "application_events WHERE application_id = :a", a=application) == 2


def test_another_persons_application_cannot_be_moved_and_is_not_found(world):
    with world.engine.begin() as conn:
        bobs = make_application(conn, world.bob, world.job)
    refused(move(world, str(bobs), "interview", datetime.now(UTC) - timedelta(minutes=1)), 404, "not_found")
    refused(move(world, str(uuid4()), "interview", datetime.now(UTC) - timedelta(minutes=1)), 404, "not_found")
    assert count(world.engine, "application_events WHERE application_id = :a", a=bobs) == 1


# ---- reading the tracker ------------------------------------------------------------------------------------------


def test_the_tracker_lists_each_application_with_its_job_and_its_history_in_the_order_it_happened(world):
    with world.engine.begin() as conn:
        interview = make_application(conn, world.ada, world.job, status="interview")
        applied = make_application(conn, world.ada, world.other)
    data = ok(world.client.get("/v1/applications"))["data"]

    assert [a["id"] for a in data] == [str(interview), str(applied)]  # the one that changed last comes first
    first, second = data
    assert (first["job_title"], first["company_name"], first["status"]) == (
        "Data engineer",
        "Initech Demo",
        "interview",
    )
    assert (second["job_title"], second["company_name"], second["status"]) == ("Analyst", "Hooli Demo", "applied")
    assert [e["status"] for e in first["events"]] == ["applied", "interview"]
    assert [e["status"] for e in second["events"]] == ["applied"]
    for event in first["events"]:
        assert set(event) == {"status", "occurred_at", "created_at"}
    applied_at, interview_at = (when(e["occurred_at"]) for e in first["events"])
    assert applied_at < interview_at and when(first["status_changed_at"]) == interview_at


def test_another_persons_applications_never_appear(world):
    with world.engine.begin() as conn:
        mine = make_application(conn, world.ada, world.job)
        make_application(conn, world.bob, world.job)
        make_application(conn, world.bob, world.other, status="offer")
    data = ok(world.client.get("/v1/applications"))["data"]
    assert [a["id"] for a in data] == [str(mine)]
    world.sign_in(world.bob)
    assert len(ok(world.client.get("/v1/applications"))["data"]) == 2


def test_a_person_with_no_applications_gets_an_empty_list(world):
    assert ok(world.client.get("/v1/applications"))["data"] == []


def test_the_chat_tool_reads_the_same_views_the_route_serialises(world):
    with world.engine.begin() as conn:
        make_application(conn, world.ada, world.job, status="rejected")
        make_application(conn, world.ada, world.other)
    served = ok(world.client.get("/v1/applications"))["data"]
    with world.engine.connect() as conn:
        views = apply_api.list_applications(conn, world.ada)
    assert views == [apply_api.ApplicationView.model_validate(item) for item in served]
    assert all(isinstance(view, apply_api.ApplicationView) for view in views)
    with world.engine.connect() as conn:
        assert apply_api.list_applications(conn, uuid4()) == []


def test_the_interface_other_modules_may_import_keeps_the_names_the_chat_tool_needs():
    assert set(apply_api.__all__) == {
        "ApplicationView",
        "list_applications",
        # appended by plan 05-05: the packet functions and view
        "PacketView",
        "current_for_job",
        "draft_packet",
        "get_packet",
        "request_packet",
    }


# ---- who may call --------------------------------------------------------------------------------------------------


def test_nobody_signed_in_is_refused_and_a_write_needs_its_key(api_client, world):
    api_client.app.dependency_overrides.pop(current_user)
    refused(api_client.get("/v1/applications"), 401, "unauthenticated")
    refused(api_client.post("/v1/applications", json={"job_id": world.job.id}), 401, "unauthenticated")
    world.sign_in(world.ada)
    refused(api_client.post("/v1/applications", json={"job_id": world.job.id}), 400, "idempotency_key_missing")
    assert count(world.engine, "applications") == 0


def test_a_person_id_in_the_body_is_ignored_the_session_decides_who_applied(world):
    body = {"job_id": world.job.id, "user_id": str(world.bob)}
    ok(post(world, "/v1/applications", body), 201)
    assert count(world.engine, "applications WHERE user_id = :u", u=world.ada) == 1
    assert count(world.engine, "applications WHERE user_id = :u", u=world.bob) == 0
