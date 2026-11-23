"""Plan 07-05, CMP-03, CMP-04, GRD-02: /v1/approvals over HTTP against the real Guardian.

The routes run as the real app_api role; the worker's half of an approval (`guardian.release`) is run through the queue
as app_worker, as the worker process would. The fake provider answers `verify.entail`. Everything is invented.
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.guardian import api as guardian
from app.guardian import jobs  # noqa: F401  (importing it registers guardian.release)
from app.platform import config, db, queue
from app.platform.sessions import Principal, current_user
from tests.guardian.support import (
    TYPED_ADDRESS,
    entails,
    make_committed_world,
    packet_payload,
    propose_follow_up,
    rows,
    scalar,
)

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
def fake_model(monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path, times=30)


@pytest.fixture
def world(api_client, engine, fake_model):
    """A committed person, job and campaign (follow-up address typed), and one follow-up waiting for approval."""
    w = make_committed_world(engine)
    p = propose_follow_up(w)
    sign_in(api_client, w.user)
    return type("W", (), {"w": w, "p": p, "client": api_client, "engine": engine})()


def payload_hash(engine, action_id: int) -> str:
    return guardian.payload_hash(scalar(engine, "SELECT payload FROM agent_actions WHERE id = :i", i=action_id))


def approve(world, body=None, *, approval=None, headers=None):
    approval = approval or world.p.approval_id
    body = {"payload_hash": payload_hash(world.engine, world.p.action_id)} if body is None else body
    return world.client.post(f"/v1/approvals/{approval}/approve", json=body, headers=headers or key())


def run_release() -> int:
    return queue.run_once(lambda: db.transaction(role="worker"), "test-worker", ["interactive"])


# ---- reading --------------------------------------------------------------------------------------------------------


def test_the_list_holds_pending_approvals_by_default_with_the_exact_payload(world):
    data = ok(world.client.get("/v1/approvals"))
    assert [a["id"] for a in data] == [str(world.p.approval_id)]
    item = data[0]
    assert (item["kind"], item["status"], item["campaign_id"]) == ("send_follow_up", "pending", str(world.w.campaign))
    assert item["payload"]["to"] == TYPED_ADDRESS and item["editable_fields"] == ["subject", "body"]
    assert item["action"] == {"id": world.p.action_id, "status": "awaiting_approval"}


def test_the_list_filters_by_status_campaign_and_kind(world):
    other = make_committed_world(world.engine, consent=True)  # someone else's pending approval never shows
    assert [a["id"] for a in ok(world.client.get("/v1/approvals", params={"status": "approved"}))] == []
    assert len(ok(world.client.get("/v1/approvals", params={"campaign_id": str(world.w.campaign)}))) == 1
    assert ok(world.client.get("/v1/approvals", params={"campaign_id": str(other.campaign)})) == []
    assert len(ok(world.client.get("/v1/approvals", params={"kind": "send_follow_up"}))) == 1
    assert ok(world.client.get("/v1/approvals", params={"kind": "submit_application"})) == []
    refused(world.client.get("/v1/approvals", params={"kind": "send_money"}), 422, "validation_failed")
    refused(world.client.get("/v1/approvals", params={"status": "maybe"}), 422, "validation_failed")


def test_the_list_paginates_with_a_cursor(world):
    for n in (2, 3):
        propose_follow_up(world.w, step=f"follow_up:{n}")
    first = world.client.get("/v1/approvals", params={"limit": 2})
    page = ok(first)
    cursor = first.json()["meta"]["next_cursor"]
    assert len(page) == 2 and cursor is not None and first.json()["meta"]["limit"] == 2
    second = world.client.get("/v1/approvals", params={"limit": 2, "cursor": cursor})
    assert len(ok(second)) == 1 and second.json()["meta"]["next_cursor"] is None
    seen = {a["id"] for a in page} | {a["id"] for a in ok(second)}
    assert len(seen) == 3
    refused(world.client.get("/v1/approvals", params={"cursor": "garbage"}), 400, "invalid_request")
    refused(world.client.get("/v1/approvals", params={"limit": 201}), 422, "validation_failed")


def test_one_approval_has_the_payload_hash_the_checks_and_the_actions_role(world):
    data = ok(world.client.get(f"/v1/approvals/{world.p.approval_id}"))
    assert data["payload_hash"] == payload_hash(world.engine, world.p.action_id)
    assert data["needs_reply"] is False and data["expires_at"] is not None
    assert data["action"] == {
        "id": world.p.action_id,
        "agent_role": "chaser",
        "status": "awaiting_approval",
        "policy_decision": "ask",
    }
    assert {c["check"] for c in data["checks"]} == {
        "citations",
        "consent",
        "scope",
        "budget",
        "injection",
        "rate_limit",
    }
    assert all(c["passed"] for c in data["checks"])


# ---- approving ------------------------------------------------------------------------------------------------------


def test_approving_records_the_yes_and_answers_release_pending(world):
    data = ok(approve(world))
    assert data["release"] == "pending"
    assert data["approval"]["status"] == "approved" and data["approval"]["note"] is None
    assert data["action"] == {"id": world.p.action_id, "type": "send_follow_up", "status": "awaiting_approval"}
    assert scalar(world.engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=world.p.action_id) == (
        "awaiting_approval"
    )
    assert scalar(world.engine, "SELECT count(*) FROM outbox") == 0
    job = rows(world.engine, "SELECT queue, unique_key FROM queue_jobs WHERE kind = 'guardian.release'")
    assert [tuple(j) for j in job] == [("interactive", f"guardian.release:{world.p.approval_id}")]


def test_after_the_release_job_the_action_is_scheduled_with_the_undo_window(world):
    ok(approve(world))
    assert run_release() == 1
    row = rows(
        world.engine,
        "SELECT status::text, extract(epoch FROM undo_until - clock_timestamp()) / 60 AS minutes "
        "FROM agent_actions WHERE id = :i",
        i=world.p.action_id,
    )[0]
    assert row.status == "scheduled" and 9.5 < float(row.minutes) <= 10  # the default window: 10 minutes
    assert scalar(world.engine, "SELECT count(*) FROM outbox WHERE agent_action_id = :i", i=world.p.action_id) == 1
    shown = ok(world.client.get(f"/v1/approvals/{world.p.approval_id}"))
    assert (shown["status"], shown["action"]["status"]) == ("approved", "scheduled")


def test_approving_with_an_edit_rejects_the_original_and_the_replacement_is_scheduled(world):
    data = ok(
        approve(
            world,
            {
                "payload_hash": payload_hash(world.engine, world.p.action_id),
                "edits": {"body": "Hello,\n\nChecking in on my application.\n\nBest regards,\nAda"},
            },
        )
    )
    assert data["approval"]["status"] == "rejected" and data["approval"]["note"] == "edited"
    assert data["action"]["status"] == "rejected" and data["release"] == "pending"

    assert run_release() == 1

    replacement = rows(
        world.engine,
        "SELECT id, status::text, payload ->> 'body' AS body, payload ->> 'to' AS recipient FROM agent_actions "
        "WHERE inputs ->> 'replaces' = :o",
        o=str(world.p.action_id),
    )
    assert len(replacement) == 1 and replacement[0].status == "scheduled"
    assert "Checking in" in replacement[0].body and replacement[0].recipient == TYPED_ADDRESS
    original = scalar(world.engine, "SELECT payload ->> 'body' FROM agent_actions WHERE id = :i", i=world.p.action_id)
    assert "Checking in" not in original  # recorder content is immutable


@pytest.mark.parametrize("field", ["to", "reply_to", "channel"])
def test_the_recipient_and_reply_to_are_never_editable(world, field):
    body = {"payload_hash": payload_hash(world.engine, world.p.action_id), "edits": {field: "thief@elsewhere.example"}}
    error = refused(approve(world, body), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == field
    assert scalar(world.engine, "SELECT status::text FROM approvals") == "pending"
    assert scalar(world.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'guardian.release'") == 0


def test_a_reply_is_refused_until_the_twin_exists(world):
    body = {"payload_hash": payload_hash(world.engine, world.p.action_id), "reply": "Yes, I would relocate."}
    error = refused(approve(world, body), 422, "validation_failed")
    assert error["details"]["fields"][0]["field"] == "reply"
    assert scalar(world.engine, "SELECT status::text FROM approvals") == "pending"


def test_a_stale_hash_is_409_preview_changed_and_nothing_is_decided(world):
    error = refused(approve(world, {"payload_hash": "0" * 64}), 409, "preview_changed")
    assert error["details"]["payload_hash"] == payload_hash(world.engine, world.p.action_id)
    assert scalar(world.engine, "SELECT status::text FROM approvals") == "pending"


def test_a_decided_approval_is_409_already_resolved(world):
    ok(approve(world))
    refused(approve(world), 409, "already_resolved")
    refused(
        world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={}, headers=key()),
        409,
        "already_resolved",
    )


def test_a_paused_campaign_is_409_conflict_with_the_state(world):
    from app.campaigns import stages

    with db.transaction(world.w.user) as conn:
        stages.pause(conn, world.w.campaign, "candidate")
    error = refused(approve(world), 409, "conflict")
    assert error["details"]["state"] == "paused"
    assert scalar(world.engine, "SELECT status::text FROM approvals") == "pending"


def test_a_retry_with_the_same_key_returns_the_first_answer_and_queues_one_release(world):
    headers = key()
    first = approve(world, headers=headers)
    again = approve(world, headers=headers)
    assert again.json()["data"] == first.json()["data"] and again.json()["meta"]["idempotent_replay"] is True
    assert scalar(world.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'guardian.release'") == 1


def test_an_edited_follow_up_that_fails_a_key_is_blocked_and_nothing_is_sent(world):
    body = {
        "payload_hash": payload_hash(world.engine, world.p.action_id),
        "edits": {"body": "Hello,\n\nCall me on 555 123 4567 about the pay.\n\nBest regards,\nAda"},
    }
    ok(approve(world, body))
    run_release()
    row = rows(
        world.engine,
        "SELECT status::text, outcome_code FROM agent_actions WHERE inputs ->> 'replaces' = :o",
        o=str(world.p.action_id),
    )[0]
    assert row.status == "blocked" and row.outcome_code == "restricted_data"
    assert scalar(world.engine, "SELECT count(*) FROM outbox") == 0


def test_a_packet_approval_keeps_its_kind_and_takes_cover_note_and_answer_edits(world):
    with db.transaction(world.w.user, role="worker") as conn:
        run = guardian.open_run(conn, world.w.user, world.w.campaign, "operator", "test-1")
        proposed = guardian.propose_action(
            conn,
            run,
            action_type="submit_application",
            payload=packet_payload(world.w.job_id),
            provenance={"fields": {}},
            inputs={"step_id": "packet"},
        )
    assert ok(world.client.get(f"/v1/approvals/{proposed.approval_id}"))["editable_fields"] == [
        "answers[].answer",
        "cover_note",
    ]
    body = {
        "payload_hash": payload_hash(world.engine, proposed.action_id),
        "edits": {"cover_note": "A short note from me.", "answers": [{"question_id": "why", "answer": "I like it."}]},
    }
    data = ok(approve(world, body, approval=proposed.approval_id))
    assert data["action"]["type"] == "submit_application" and data["approval"]["note"] == "edited"


# ---- rejecting ------------------------------------------------------------------------------------------------------


def test_rejecting_records_the_note_and_nothing_leaves(world):
    data = ok(
        world.client.post(
            f"/v1/approvals/{world.p.approval_id}/reject", json={"note": "I heard back by phone."}, headers=key()
        )
    )
    assert data["approval"]["status"] == "rejected" and data["approval"]["note"] == "I heard back by phone."
    assert data["action"] == {"id": world.p.action_id, "type": "send_follow_up", "status": "rejected"}
    assert "release" not in data
    assert scalar(world.engine, "SELECT count(*) FROM outbox") == 0
    assert scalar(world.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'guardian.release'") == 0


def test_rejecting_without_a_body_works_and_a_long_note_is_refused(world):
    refused(
        world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={"note": "x" * 501}, headers=key()),
        422,
        "validation_failed",
    )
    data = ok(world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={}, headers=key()))
    assert data["approval"]["note"] is None


# ---- other people and the API login ---------------------------------------------------------------------------------


def test_another_persons_approval_is_404_everywhere(world):
    stranger = make_committed_world(world.engine)
    sign_in(world.client, stranger.user)
    refused(world.client.get(f"/v1/approvals/{world.p.approval_id}"), 404, "not_found")
    refused(approve(world), 404, "not_found")
    refused(world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={}, headers=key()), 404, "not_found")
    assert ok(world.client.get("/v1/approvals")) == []
    assert scalar(world.engine, "SELECT status::text FROM approvals WHERE id = :a", a=world.p.approval_id) == "pending"


def test_every_write_needs_an_idempotency_key(world):
    body = {"payload_hash": payload_hash(world.engine, world.p.action_id)}
    refused(
        world.client.post(f"/v1/approvals/{world.p.approval_id}/approve", json=body), 400, "idempotency_key_missing"
    )
    refused(world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={}), 400, "idempotency_key_missing")


@pytest.fixture
def api_login_only(monkeypatch):
    """The worker's and the match module's URLs point at nothing, as in the API container."""
    current = config.get()
    changed = replace(current, database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})
    yield
    for made in db._engines.values():
        made.dispose()


def test_listing_reading_and_approving_need_only_the_api_login(world, api_login_only):
    ok(world.client.get("/v1/approvals"))
    ok(world.client.get(f"/v1/approvals/{world.p.approval_id}"))
    ok(approve(world))
    assert scalar(world.engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'guardian.release'") == 1
    assert set(db._engines) == {"api"}


def test_rejecting_needs_only_the_api_login(world, api_login_only):
    ok(world.client.post(f"/v1/approvals/{world.p.approval_id}/reject", json={"note": "No."}, headers=key()))
    assert set(db._engines) == {"api"}
