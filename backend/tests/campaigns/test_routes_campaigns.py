"""Plan 07-05, CMP-01, CMP-04, CMP-07, GRD-07: POST /v1/campaigns, PATCH, pause, resume and cancel over HTTP.

The routes run as the real app_api role through the app's own connections, so row-level security and the grants are in
force and what a test commits stays (api_client empties the tables around each test). A person is signed in by
overriding the session dependency. Everything is invented. The last tests run the routes with the worker's and the
match module's URLs unreachable: the API process holds only the API login, so a route that opened either would fail.
"""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.campaigns import stages
from app.platform import config, db
from app.platform.sessions import Principal, current_user
from app.trust import api as trust
from tests.campaigns.factories import make_campaign, make_step
from tests.guardian.support import (
    approve_and_release,
    entails,
    follow_up_payload,
    make_committed_world,
    propose_follow_up,
    rows,
    scalar,
)
from tests.hiring.world import exclude_company
from tests.match.factories import make_job, make_user

UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


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


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid4().hex}


def sign_in(client, user) -> None:
    client.app.dependency_overrides[current_user] = lambda: Principal(user)


@pytest.fixture
def world(api_client, engine) -> SimpleNamespace:
    """Ada (consent given), Bob, an open job at an invented company, and a second one. Ada is signed in."""
    w = SimpleNamespace(client=api_client, engine=engine)
    with engine.begin() as conn:
        w.ada, w.bob = make_user(conn), make_user(conn)
        trust.grant_consent(conn, w.ada, "ai_processing", trust.ai_processing_text_version())
        trust.grant_consent(conn, w.bob, "ai_processing", trust.ai_processing_text_version())
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
    sign_in(api_client, w.ada)
    return w


def start(world, job=None, **body):
    return world.client.post("/v1/campaigns", json={"job_id": (job or world.job).id, **body}, headers=key())


def resume_jobs(engine, campaign: str) -> list[str]:
    return [
        r[0]
        for r in rows(
            engine,
            "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume' AND payload ->> 'campaign_id' = :c "
            "ORDER BY id",
            c=campaign,
        )
    ]


# ---- starting ------------------------------------------------------------------------------------------------------


def test_starting_gives_a_researching_campaign_at_the_default_budget_and_queues_one_resume_job(world):
    data = ok(start(world), 201)

    assert data["stage"] == "researching" and data["paused_at"] is None and data["pause_reason"] is None
    assert data["job"] == {"id": world.job.id, "title": "Data engineer", "company_name": "Initech Demo"}
    assert (data["budget_usd"], data["spent_usd"], data["follow_up_after_days"]) == ("0.75", "0.000000", 7)
    assert data["next_step"] == {
        "step_key": "research",
        "agent_role": "researcher",
        "text": "Building a cited company dossier.",
    }
    stored = rows(world.engine, "SELECT user_id, stage::text, budget_microusd FROM campaigns")
    assert [tuple(r) for r in stored] == [(world.ada, "researching", 750_000)]
    assert resume_jobs(world.engine, data["id"]) == [f"campaign.resume:{data['id']}:start"]
    job = rows(world.engine, "SELECT queue, max_attempts, user_id FROM queue_jobs WHERE kind = 'campaign.resume'")[0]
    assert tuple(job) == ("default", 20, world.ada)


def test_a_budget_is_stored_in_micro_dollars(world):
    data = ok(start(world, budget_usd="1.50"), 201)
    assert data["budget_usd"] == "1.50"
    assert scalar(world.engine, "SELECT budget_microusd FROM campaigns") == 1_500_000


def test_a_retry_with_the_same_key_returns_the_first_answer_and_creates_nothing(world):
    headers = key()
    first = world.client.post("/v1/campaigns", json={"job_id": world.job.id}, headers=headers)
    again = world.client.post("/v1/campaigns", json={"job_id": world.job.id}, headers=headers)
    assert again.status_code == 201 and again.json()["data"] == first.json()["data"]
    assert again.json()["meta"]["idempotent_replay"] is True
    assert scalar(world.engine, "SELECT count(*) FROM campaigns") == 1
    assert scalar(world.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'campaign.resume'") == 1


def test_a_second_start_for_the_same_job_is_409_with_the_open_campaigns_id(world):
    first = ok(start(world), 201)
    error = refused(start(world), 409, "conflict")
    assert error["details"]["campaign_id"] == first["id"]
    assert scalar(world.engine, "SELECT count(*) FROM campaigns") == 1


def test_a_job_can_be_started_again_after_its_campaign_closed(world):
    first = ok(start(world), 201)
    ok(world.client.post(f"/v1/campaigns/{first['id']}/cancel", json={}, headers=key()))
    second = ok(start(world), 201)
    assert second["id"] != first["id"]


def test_a_closed_job_is_410(world):
    with world.engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": world.job.id})
    refused(start(world), 410, "job_closed")
    assert scalar(world.engine, "SELECT count(*) FROM campaigns") == 0


def test_a_job_that_breaks_a_hard_constraint_is_422(world):
    with world.engine.begin() as conn:
        exclude_company(conn, world.ada, "Initech Demo")
    error = refused(start(world), 422, "violates_constraint")
    assert error["details"]["kinds"] == ["excluded_company"]
    assert scalar(world.engine, "SELECT count(*) FROM campaigns") == 0


def test_without_ai_processing_consent_the_platforms_consent_error_comes_back(world):
    sign_in(world.client, _person_without_consent(world))
    error = refused(start(world), 403, "consent_required")
    assert error["details"]["purpose"] == "ai_processing"


def _person_without_consent(world):
    with world.engine.begin() as conn:
        return make_user(conn)


def test_an_unknown_job_is_404(world):
    refused(world.client.post("/v1/campaigns", json={"job_id": 987654321}, headers=key()), 404, "not_found")


@pytest.mark.parametrize("budget", ["0", "-1", "100.01", "1.0000001", "abc", "NaN", "Infinity", True])
def test_a_budget_of_zero_negative_too_large_or_too_precise_is_422(world, budget):
    refused(start(world, budget_usd=budget), 422, "validation_failed")
    assert scalar(world.engine, "SELECT count(*) FROM campaigns") == 0


@pytest.mark.parametrize("budget", ["100", "0.000001"])
def test_the_edges_of_the_budget_range_are_accepted(world, budget):
    ok(start(world, budget_usd=budget), 201)


def test_unknown_fields_and_a_missing_key_are_refused(world):
    refused(start(world, surprise=1), 422, "validation_failed")
    refused(world.client.post("/v1/campaigns", json={"job_id": world.job.id}), 400, "idempotency_key_missing")


# ---- changing ------------------------------------------------------------------------------------------------------


def patch(world, campaign: str, **body):
    return world.client.patch(f"/v1/campaigns/{campaign}", json=body, headers=key())


def test_patch_changes_the_budget_days_and_address_and_wakes_the_campaign_once_per_change(world):
    campaign = ok(start(world), 201)["id"]

    data = ok(
        patch(world, campaign, budget_usd="2", follow_up_after_days=10, follow_up_email="  Talent@Lumen.Example ")
    )

    assert (data["budget_usd"], data["follow_up_after_days"], data["follow_up_email"]) == (
        "2.00",
        10,
        "Talent@Lumen.Example",
    )
    woken = resume_jobs(world.engine, campaign)[1:]  # the first is the start
    assert [w.split(":")[2].split(".")[0] for w in woken] == ["budget", "days", "address"]
    assert len(set(woken)) == 3
    ok(patch(world, campaign, budget_usd="3"))
    assert len(resume_jobs(world.engine, campaign)) == 5  # a second change wakes it again (its cause is new)


def test_patch_can_clear_the_address(world):
    campaign = ok(start(world), 201)["id"]
    ok(patch(world, campaign, follow_up_email="talent@lumen.example"))
    assert ok(patch(world, campaign, follow_up_email=None))["follow_up_email"] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"budget_usd": "0"},
        {"follow_up_after_days": 0},
        {"follow_up_after_days": 61},
        {"follow_up_email": "not an address"},
        {"follow_up_email": "a@b"},
        {"follow_up_email": "x" * 250 + "@a.example"},
        {"stage": "offer"},
    ],
)
def test_patch_refuses_what_the_database_would_and_unknown_fields(world, body):
    campaign = ok(start(world), 201)["id"]
    before = len(resume_jobs(world.engine, campaign))
    error = refused(patch(world, campaign, **body), 422, "validation_failed") if body else None
    assert error is None or error["details"]
    assert len(resume_jobs(world.engine, campaign)) == before


def test_an_empty_patch_changes_and_wakes_nothing(world):
    campaign = ok(start(world), 201)["id"]
    ok(patch(world, campaign))
    assert len(resume_jobs(world.engine, campaign)) == 1


def test_patching_a_closed_campaign_is_409_with_the_state(world):
    campaign = ok(start(world), 201)["id"]
    ok(world.client.post(f"/v1/campaigns/{campaign}/cancel", json={}, headers=key()))
    assert refused(patch(world, campaign, budget_usd="2"), 409, "conflict")["details"]["state"] == "closed"


# ---- pause and resume ----------------------------------------------------------------------------------------------


def post(world, campaign: str, action: str, body: dict | None = None):
    return world.client.post(f"/v1/campaigns/{campaign}/{action}", json=body or {}, headers=key())


def test_pause_stops_the_campaign_with_reason_candidate_and_resume_starts_it_again(world):
    campaign = ok(start(world), 201)["id"]

    paused = ok(post(world, campaign, "pause"))

    assert (paused["stage"], paused["pause_reason"]) == ("researching", "candidate")
    assert paused["paused_at"] is not None and paused["held"] == {"running_steps_stopping": 0, "follow_ups_held": 0}
    assert "nothing is sent until you resume" in paused["effective"]
    assert scalar(world.engine, "SELECT paused_at IS NOT NULL FROM campaigns") is True
    assert ok(post(world, campaign, "pause"))["pause_reason"] == "candidate"  # again: the current state
    assert any(".pause." in k or k.split(":")[2].startswith("pause.") for k in resume_jobs(world.engine, campaign))

    resumed = ok(post(world, campaign, "resume"))

    assert (resumed["paused_at"], resumed["pause_reason"]) == (None, None)
    assert any(k.split(":")[2].startswith("unpause.") for k in resume_jobs(world.engine, campaign))
    assert ok(post(world, campaign, "resume"))["paused_at"] is None  # not paused: nothing to do


def test_pause_counts_running_steps_and_holds_a_scheduled_follow_up(engine, api_client, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    follow = make_committed_world(engine)
    with engine.begin() as conn:
        make_step(conn, follow.campaign, "research", status="running")
    p = propose_follow_up(follow)
    assert approve_and_release(follow, p) == "scheduled"
    sign_in(api_client, follow.user)

    paused = ok(post(SimpleNamespace(client=api_client), str(follow.campaign), "pause"))

    assert paused["held"] == {"running_steps_stopping": 1, "follow_ups_held": 1}
    assert scalar(engine, "SELECT not_before::text FROM outbox WHERE agent_action_id = :a", a=p.action_id) == "infinity"
    ok(post(SimpleNamespace(client=api_client), str(follow.campaign), "resume"))
    assert scalar(engine, "SELECT not_before::text FROM outbox WHERE agent_action_id = :a", a=p.action_id) != "infinity"


def test_a_campaign_paused_at_its_budget_cannot_resume_until_the_budget_is_raised(world):
    campaign = ok(start(world), 201)["id"]
    with db.transaction(world.ada) as conn:
        stages.pause(conn, campaign, "budget_exhausted")
    with world.engine.begin() as conn:
        _spend(conn, world.ada, campaign, 750_000)

    assert ok(post(world, campaign, "pause"))["pause_reason"] == "budget_exhausted"  # already paused: its state
    error = refused(post(world, campaign, "resume"), 409, "budget_exhausted")
    assert error["details"]["budget_usd"] == "0.75"
    assert scalar(world.engine, "SELECT paused_at IS NOT NULL FROM campaigns") is True

    ok(patch(world, campaign, budget_usd="1.50"))
    resumed = ok(post(world, campaign, "resume"))
    assert resumed["paused_at"] is None


def _spend(conn, user, campaign, micro: int) -> None:
    model = conn.execute(
        text(
            """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
               VALUES ('local', 'test-model-' || gen_random_uuid(), 'chat', true, 'test') RETURNING id"""
        )
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id, cost_microusd,
                                      latency_ms) VALUES ('research.plan', 'v1', :m, 'ok', :u, :c, :cost, 1)"""
        ),
        {"m": model, "u": user, "c": campaign, "cost": micro},
    )


def test_a_scam_pause_resumes_as_the_candidates_override(world):
    campaign = ok(start(world), 201)["id"]
    with db.transaction(world.ada) as conn:
        stages.pause(conn, campaign, "scam_suspected", details={"signals": [{"kind": "fee", "quote": "send a fee"}]})
    assert ok(post(world, campaign, "resume"))["paused_at"] is None


def test_pause_and_resume_of_a_closed_campaign_are_409(world):
    campaign = ok(start(world), 201)["id"]
    ok(post(world, campaign, "cancel"))
    for action in ("pause", "resume"):
        assert refused(post(world, campaign, action), 409, "conflict")["details"]["state"] == "closed"


# ---- cancelling ----------------------------------------------------------------------------------------------------


def test_cancel_closes_the_campaign_and_returns_what_it_did(world):
    campaign = ok(start(world), 201)["id"]

    data = ok(post(world, campaign, "cancel", {"note": "Took another offer."}))

    assert (data["stage"], data["closed_reason"], data["introduction_id"]) == ("closed", "cancelled", None)
    assert data["closed_at"] is not None and (data["expired_approvals"], data["cancelled_actions"]) == (0, 0)
    stored = rows(world.engine, "SELECT stage::text, closed_reason::text, paused_at FROM campaigns")[0]
    assert tuple(stored) == ("closed", "cancelled", None)
    assert any(k.endswith(":cancel") for k in resume_jobs(world.engine, campaign))
    refused(post(world, campaign, "cancel"), 409, "conflict")


def test_cancel_expires_pending_approvals_with_the_note_and_cancels_a_scheduled_follow_up(
    engine, api_client, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path, times=24)
    w = make_committed_world(engine)
    scheduled = propose_follow_up(w, step="follow_up:1")
    assert approve_and_release(w, scheduled) == "scheduled"
    waiting = propose_follow_up(w, step="follow_up:2", payload=follow_up_payload("I applied on 3 November 2026."))
    sign_in(api_client, w.user)

    data = ok(post(SimpleNamespace(client=api_client), str(w.campaign), "cancel", {"note": "Took another offer."}))

    assert data["cancelled_actions"] >= 1 and data["expired_approvals"] == 1
    note = scalar(engine, "SELECT note FROM approvals WHERE id = :a", a=waiting.approval_id)
    assert note == "Took another offer."
    assert scalar(engine, "SELECT status::text FROM approvals WHERE id = :a", a=waiting.approval_id) == "expired"
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=scheduled.action_id) == "cancelled"
    assert scalar(engine, "SELECT status::text FROM outbox WHERE agent_action_id = :a", a=scheduled.action_id) == (
        "cancelled"
    )


@pytest.mark.parametrize("reason", ["offer_accepted", "offer_declined"])
def test_an_offer_reason_is_accepted_only_at_the_offer_stage(world, reason):
    campaign = ok(start(world), 201)["id"]
    error = refused(post(world, campaign, "cancel", {"reason": reason}), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "reason"
    with world.engine.begin() as conn:
        conn.execute(text("UPDATE campaigns SET stage = 'offer' WHERE id = :c"), {"c": campaign})
    data = ok(post(world, campaign, "cancel", {"reason": reason}))
    assert data["closed_reason"] == reason


def test_cancelling_a_campaign_started_by_an_introduction_returns_the_introduction_id_unchanged(world):
    from tests.hiring.factories import make_accepted, make_ask, make_candidate, make_open_role, make_org

    with world.engine.begin() as conn:
        org = make_org(conn)
        role = make_open_role(conn, org)
        candidate = make_candidate(conn)
        accepted = make_accepted(conn, role, candidate, ask=make_ask(conn, role, candidate))
        campaign = make_campaign(conn, candidate.id, role.job_id, stage="applied", introduction_id=accepted.ask.id)
    sign_in(world.client, candidate.id)

    data = ok(post(world, str(campaign), "cancel"))

    assert data["introduction_id"] == str(accepted.ask.id)
    status = scalar(world.engine, "SELECT status::text FROM introductions WHERE id = :i", i=accepted.ask.id)
    assert status == "accepted"


# ---- other people and the API login ---------------------------------------------------------------------------------


def test_another_persons_campaign_is_404_on_every_endpoint(world):
    campaign = ok(start(world), 201)["id"]
    sign_in(world.client, world.bob)
    refused(patch(world, campaign, budget_usd="2"), 404, "not_found")
    for action in ("pause", "resume", "cancel"):
        refused(post(world, campaign, action), 404, "not_found")
    assert scalar(world.engine, "SELECT stage::text FROM campaigns") == "researching"
    assert scalar(world.engine, "SELECT budget_microusd FROM campaigns") == 750_000


def test_every_write_needs_an_idempotency_key(world):
    campaign = ok(start(world), 201)["id"]
    for call in (
        lambda: world.client.patch(f"/v1/campaigns/{campaign}", json={"budget_usd": "2"}),
        lambda: world.client.post(f"/v1/campaigns/{campaign}/pause", json={}),
        lambda: world.client.post(f"/v1/campaigns/{campaign}/resume", json={}),
        lambda: world.client.post(f"/v1/campaigns/{campaign}/cancel", json={}),
    ):
        refused(call(), 400, "idempotency_key_missing")


@pytest.fixture
def api_login_only(monkeypatch):
    """The worker's and the match module's URLs point at nothing, as in the API container: a route that opened either
    login would fail. The engines are made again from the changed settings and disposed afterwards."""
    current = config.get()
    changed = replace(current, database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})
    yield
    for made in db._engines.values():
        made.dispose()


def test_every_campaign_write_runs_on_the_api_login_alone(world, api_login_only):
    campaign = ok(start(world, budget_usd="1.00"), 201)["id"]
    ok(patch(world, campaign, budget_usd="2", follow_up_after_days=9, follow_up_email="talent@lumen.example"))
    ok(post(world, campaign, "pause"))
    ok(post(world, campaign, "resume"))
    ok(post(world, campaign, "cancel", {"note": "Done."}))
    assert set(db._engines) == {"api"}
