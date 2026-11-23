"""Plan 07-05, CMP-01, CMP-04: the outside events that move a campaign, each exactly once.

Hiring, apply and the Guardian publish; `app.campaigns.subscribers` turns each event into a start or one
`campaign.resume` job. The graph is not run: the tests assert the rows written and the jobs queued. A subscriber job is
run by calling its registered handler with the payload stored in the queue (the queue's own claim would also pick up the
resume jobs, whose handler is plan 07-04's). Rows are invented.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.campaigns import subscribers  # noqa: F401  (importing it registers the handlers)
from app.platform import db, events, queue
from app.platform.sessions import Principal, current_user
from tests.campaigns.factories import make_campaign
from tests.guardian.support import entails, make_committed_world, propose_follow_up
from tests.hiring import world as hiring_world
from tests.hiring.test_introduction_routes import accept_body, act, scene
from tests.p5_factories import make_application


def rows(engine, sql: str, **params):
    with engine.connect() as c:
        return list(c.execute(text(sql), params).all())


def scalar(engine, sql: str, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


def scene_for_campaign(engine):
    """An organization with an open role and one candidate who was asked (not yet accepted)."""
    return scene(engine)


def accept_introduction(client, s) -> None:
    act(client, s.candidate.id)
    response = client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key())
    assert response.status_code == 200, response.text


def run_jobs(engine, kind: str) -> int:
    """Run every queued job of this kind once, as the worker would (its handler, on the stored payload)."""
    payloads = [r[0] for r in rows(engine, "SELECT payload FROM queue_jobs WHERE kind = :k ORDER BY id", k=kind)]
    for payload in payloads:
        with db.transaction(role="worker") as conn:
            queue._handlers[kind](conn, payload)
    return len(payloads)


def resume_keys(engine) -> list[str]:
    return [r[0] for r in rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume' ORDER BY id")]


# ---- the registry and introduction.accepted -------------------------------------------------------------------------


def test_the_four_events_are_subscribed_in_both_processes():
    assert events.subscribers("introduction.accepted") == [("campaign.from_introduction", "default")]
    assert events.subscribers("introduction.answered") == [("campaign.introduction_answered", "default")]
    assert events.subscribers("application.status_changed") == [("campaign.application_changed", "default")]
    assert ("campaign.resume", "default") in events.subscribers("guardian.action_changed")
    for kind in ("campaign.from_introduction", "campaign.introduction_answered", "campaign.application_changed"):
        assert kind in queue._handlers


def test_accepting_publishes_one_job_in_the_accept_transaction_and_a_failed_accept_publishes_nothing(
    engine, api_client
):
    s = scene_for_campaign(engine)
    act(api_client, s.candidate.id)
    stale = {"share_preview_hash": "0" * 64, "consent_text_version": accept_body(s.ask)["consent_text_version"]}
    assert api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=stale, headers=key()).status_code == 409
    assert scalar(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'campaign.from_introduction'") == 0

    accept_introduction(api_client, s)

    job = rows(engine, "SELECT unique_key, queue, payload, user_id FROM queue_jobs WHERE kind LIKE 'campaign.%'")
    assert len(job) == 1
    assert job[0].unique_key == f"campaign.from_introduction:{s.ask.id}"
    assert job[0].payload == {"introduction_id": str(s.ask.id), "user_id": str(s.candidate.id)}
    assert (job[0].queue, job[0].user_id) == ("default", s.candidate.id)


def test_the_job_creates_one_campaign_at_applied_and_running_it_again_creates_no_second(engine, api_client):
    s = scene_for_campaign(engine)
    accept_introduction(api_client, s)

    assert run_jobs(engine, "campaign.from_introduction") == 1
    assert run_jobs(engine, "campaign.from_introduction") == 1  # the same delivery twice

    stored = rows(engine, "SELECT user_id, job_id, introduction_id, stage::text, budget_microusd FROM campaigns")
    assert [tuple(r) for r in stored] == [(s.candidate.id, s.role.job_id, s.ask.id, "applied", 750_000)]
    campaign = scalar(engine, "SELECT id FROM campaigns")
    assert resume_keys(engine) == [f"campaign.resume:{campaign}:start"]


def test_an_open_campaign_for_the_roles_job_takes_the_introduction_and_none_is_created(engine, api_client):
    s = scene_for_campaign(engine)
    with engine.begin() as conn:
        existing = make_campaign(conn, s.candidate.id, s.role.job_id, stage="researching")
    accept_introduction(api_client, s)

    run_jobs(engine, "campaign.from_introduction")

    assert scalar(engine, "SELECT count(*) FROM campaigns") == 1
    assert scalar(engine, "SELECT introduction_id FROM campaigns WHERE id = :c", c=existing) == s.ask.id
    assert scalar(engine, "SELECT stage::text FROM campaigns WHERE id = :c", c=existing) == "researching"


def test_an_event_naming_another_person_changes_nothing_of_theirs(engine, api_client):
    s = scene_for_campaign(engine)
    accept_introduction(api_client, s)
    with engine.begin() as conn:
        stranger = hiring_world.candidate(conn)
    payload = {"introduction_id": str(s.ask.id), "user_id": str(stranger)}

    with db.transaction(role="worker") as conn:
        queue._handlers["campaign.from_introduction"](conn, payload)

    assert scalar(engine, "SELECT count(*) FROM campaigns") == 0
    assert resume_keys(engine) == []


def test_an_introduction_that_is_not_accepted_starts_nothing(engine):
    s = scene_for_campaign(engine)
    payload = {"introduction_id": str(s.ask.id), "user_id": str(s.candidate.id)}
    with db.transaction(role="worker") as conn:
        queue._handlers["campaign.from_introduction"](conn, payload)
    assert scalar(engine, "SELECT count(*) FROM campaigns") == 0


def test_the_queue_itself_runs_the_subscriber_job(engine, api_client):
    s = scene_for_campaign(engine)
    accept_introduction(api_client, s)
    with engine.begin() as conn:  # the resume handler is plan 07-04's: only the subscriber job is claimed here
        conn.execute(text("UPDATE queue_jobs SET queue = 'later' WHERE kind <> 'campaign.from_introduction'"))

    assert queue.run_once(lambda: db.transaction(role="worker"), "test-worker", ["default"]) == 1

    assert scalar(engine, "SELECT stage::text FROM campaigns") == "applied"
    assert scalar(engine, "SELECT status::text FROM queue_jobs WHERE kind = 'campaign.from_introduction'") == (
        "succeeded"
    )


# ---- introduction.answered ------------------------------------------------------------------------------------------


def answer(client, s, verdict: str = "yes"):
    act(client, s.member)
    response = client.post(
        f"/v1/introductions/{s.ask.id}/answer", json={"answer": verdict, "reason": "A demo reason."}, headers=key()
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("verdict", ["yes", "no"])
def test_an_employer_answer_wakes_the_linked_campaign_with_the_answer_in_the_cause(engine, api_client, verdict):
    s = scene(engine, accepted=True)
    with engine.begin() as conn:
        campaign = make_campaign(conn, s.candidate.id, s.role.job_id, stage="applied", introduction_id=s.ask.id)

    answer(api_client, s, verdict)
    job = rows(
        engine, "SELECT unique_key, payload, user_id FROM queue_jobs WHERE kind = 'campaign.introduction_answered'"
    )
    assert len(job) == 1 and job[0].unique_key == f"campaign.introduction_answered:{s.ask.id}"
    assert job[0].payload == {"introduction_id": str(s.ask.id), "user_id": str(s.candidate.id), "answer": verdict}
    assert job[0].user_id == s.candidate.id

    run_jobs(engine, "campaign.introduction_answered")
    run_jobs(engine, "campaign.introduction_answered")  # twice

    assert resume_keys(engine) == [f"campaign.resume:{campaign}:intro{s.ask.id}.{verdict}"]


def test_an_answer_for_an_introduction_with_no_campaign_wakes_nothing(engine, api_client):
    s = scene(engine, accepted=True)
    answer(api_client, s)
    assert run_jobs(engine, "campaign.introduction_answered") == 1
    assert resume_keys(engine) == []


def test_an_answer_event_naming_another_person_wakes_nothing(engine, api_client):
    s = scene(engine, accepted=True)
    with engine.begin() as conn:
        make_campaign(conn, s.candidate.id, s.role.job_id, stage="applied", introduction_id=s.ask.id)
        stranger = hiring_world.candidate(conn)
    answer(api_client, s)
    payload = {"introduction_id": str(s.ask.id), "user_id": str(stranger), "answer": "yes"}
    with db.transaction(role="worker") as conn:
        queue._handlers["campaign.introduction_answered"](conn, payload)
    assert resume_keys(engine) == []


# ---- application.status_changed -------------------------------------------------------------------------------------


@pytest.fixture
def tracked(api_client, engine):
    """Ada with a job and an open campaign for it, signed in; no application yet."""
    from types import SimpleNamespace

    from tests.match.factories import make_job, make_user

    w = SimpleNamespace(client=api_client, engine=engine)
    with engine.begin() as conn:
        w.ada, w.bob = make_user(conn), make_user(conn)
        w.job = make_job(
            conn, title="Data engineer", description="Build pipelines.", requirements=[("Build", "required")]
        )
        w.other = make_job(
            conn,
            title="Analyst",
            company_name="Hooli Demo",
            description="Analyse data.",
            requirements=[("Analyse", "required")],
        )
        w.campaign = make_campaign(conn, w.ada, w.job.id, stage="researching")
    api_client.app.dependency_overrides[current_user] = lambda: Principal(w.ada)
    return w


def record(w, job, when: str = "2026-11-20T09:30:00Z"):
    response = w.client.post("/v1/applications", json={"job_id": job.id, "occurred_at": when}, headers=key())
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_recording_applied_publishes_one_job_that_links_the_application_and_wakes_the_campaign(tracked):
    w = tracked
    application = record(w, w.job)
    job = rows(w.engine, "SELECT payload, user_id FROM queue_jobs WHERE kind = 'campaign.application_changed'")
    assert len(job) == 1 and job[0].user_id == w.ada
    event = scalar(w.engine, "SELECT id FROM application_events")
    assert job[0].payload == {
        "application_id": application["id"],
        "user_id": str(w.ada),
        "job_id": w.job.id,
        "status": "applied",
        "application_event_id": event,
    }

    run_jobs(w.engine, "campaign.application_changed")
    run_jobs(w.engine, "campaign.application_changed")

    assert str(scalar(w.engine, "SELECT campaign_id FROM applications")) == str(w.campaign)
    assert resume_keys(w.engine) == [f"campaign.resume:{w.campaign}:app{event}"]


def test_each_later_status_wakes_the_campaign_with_its_own_event(tracked):
    w = tracked
    application = record(w, w.job)
    run_jobs(w.engine, "campaign.application_changed")
    moved = w.client.post(
        f"/v1/applications/{application['id']}/status",
        json={"status": "interview", "occurred_at": "2026-11-21T09:30:00Z"},
        headers=key(),
    )
    assert moved.status_code == 201, moved.text
    run_jobs(w.engine, "campaign.application_changed")

    events_ = [r[0] for r in rows(w.engine, "SELECT id FROM application_events ORDER BY id")]
    assert resume_keys(w.engine) == [f"campaign.resume:{w.campaign}:app{e}" for e in events_]
    assert len(events_) == 2


def test_a_status_for_a_job_with_no_campaign_wakes_nothing(tracked):
    w = tracked
    record(w, w.other)
    assert run_jobs(w.engine, "campaign.application_changed") == 1
    assert resume_keys(w.engine) == []
    assert scalar(w.engine, "SELECT campaign_id FROM applications") is None


def test_an_application_event_naming_another_person_wakes_nothing(tracked):
    w = tracked
    record(w, w.job)
    event = scalar(w.engine, "SELECT id FROM application_events")
    payload = {
        "application_id": str(uuid4()),
        "user_id": str(w.bob),
        "job_id": w.job.id,
        "status": "applied",
        "application_event_id": event,
    }
    with db.transaction(role="worker") as conn:
        queue._handlers["campaign.application_changed"](conn, payload)
    assert resume_keys(w.engine) == []
    assert scalar(w.engine, "SELECT campaign_id FROM applications") is None


def test_a_refused_record_publishes_nothing(tracked):
    w = tracked
    record(w, w.job)
    again = w.client.post("/v1/applications", json={"job_id": w.job.id}, headers=key())
    assert again.status_code == 409
    assert scalar(w.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'campaign.application_changed'") == 1


def test_a_linked_application_wakes_its_own_campaign_even_when_another_is_open_for_the_job(tracked):
    w = tracked
    with w.engine.begin() as conn:
        application = make_application(conn, w.ada, w.job)
        conn.execute(
            text("UPDATE applications SET campaign_id = :c WHERE id = :a"), {"c": w.campaign, "a": application}
        )
    w.client.post(
        f"/v1/applications/{application}/status",
        json={"status": "offer", "occurred_at": "2026-11-21T09:30:00Z"},
        headers=key(),
    )
    run_jobs(w.engine, "campaign.application_changed")
    assert len(resume_keys(w.engine)) == 1


# ---- guardian.action_changed ----------------------------------------------------------------------------------------


def test_a_guardian_action_change_queues_exactly_one_resume_job_even_published_twice(
    engine, clean, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path)
    w = make_committed_world(engine)
    p = propose_follow_up(w)
    from app.guardian import api as guardian

    for _ in range(2):
        with db.transaction(w.user) as conn:
            events.publish(
                conn,
                "guardian.action_changed",
                {
                    "campaign_id": str(w.campaign),
                    "cause": f"a{p.action_id}.cancelled",
                    "agent_action_id": p.action_id,
                    "status": "cancelled",
                },
                key=f"{w.campaign}:a{p.action_id}.cancelled",
                user_id=w.user,
            )

    # the proposal itself already published `awaiting_approval`; the twice-published `cancelled` is one job
    assert resume_keys(engine) == [
        f"campaign.resume:{w.campaign}:a{p.action_id}.awaiting_approval",
        f"campaign.resume:{w.campaign}:a{p.action_id}.cancelled",
    ]
    payload = rows(engine, "SELECT payload FROM queue_jobs WHERE kind = 'campaign.resume' ORDER BY id")[1][0]
    assert payload["campaign_id"] == str(w.campaign) and payload["cause"] == f"a{p.action_id}.cancelled"

    # and the Guardian's own publish (a rejection) reaches the same subscriber
    with db.transaction(w.user) as conn:
        guardian.decide(
            conn, w.user, p.approval_id, approve=False, payload_hash=guardian.payload_hash(_payload(engine, p))
        )
    assert resume_keys(engine)[-1] == f"campaign.resume:{w.campaign}:a{p.action_id}.rejected"


def _payload(engine, p):
    return scalar(engine, "SELECT payload FROM agent_actions WHERE id = :i", i=p.action_id)


@pytest.fixture
def clean(engine):
    from tests.conftest import DATA_TABLES

    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))


# ---- the publish lines run on the API login alone -------------------------------------------------------------------


@pytest.fixture
def api_login_only(monkeypatch):
    from dataclasses import replace

    from app.platform import config

    current = config.get()
    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
    monkeypatch.setattr(
        config, "get", lambda: replace(current, database_url_worker=unreachable, database_url_match=unreachable)
    )
    monkeypatch.setattr(db, "_engines", {})
    yield
    for made in db._engines.values():
        made.dispose()


def test_accepting_answering_and_recording_a_status_publish_on_the_api_login_alone(engine, api_client, api_login_only):
    s = scene(engine, accepted=False)
    accept_introduction(api_client, s)
    act(api_client, s.member)
    response = api_client.post(
        f"/v1/introductions/{s.ask.id}/answer", json={"answer": "yes", "reason": "A demo reason."}, headers=key()
    )
    assert response.status_code == 200, response.text
    with engine.begin() as conn:
        job = make_application_job(conn, s.candidate.id)
    act(api_client, s.candidate.id)
    recorded = api_client.post("/v1/applications", json={"job_id": job}, headers=key())
    assert recorded.status_code == 201, recorded.text
    assert set(db._engines) == {"api"}
    kinds = {r[0] for r in rows(engine, "SELECT kind FROM queue_jobs WHERE kind LIKE 'campaign.%'")}
    assert kinds == {"campaign.from_introduction", "campaign.introduction_answered", "campaign.application_changed"}


def make_application_job(conn, user_id) -> int:
    from tests.match.factories import make_job

    return make_job(conn, title="Analyst", description="Analyse data.", requirements=[("Analyse", "required")]).id
