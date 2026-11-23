"""Plan 07-05, GRD-03, GRD-04: the flight recorder routes and undo over HTTP.

`GET /v1/control/recorder` lists a campaign's steps in order, `GET /v1/control/recorder/{id}` returns one in full (it
never calls a model), `POST /v1/control/actions/{id}/undo` takes back a follow-up inside its window. Everything is
invented; the fake provider answers `verify.entail`.
"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.guardian import jobs  # noqa: F401  (importing it registers guardian.release)
from app.platform import config, db
from tests.campaigns.factories import make_action, make_run
from tests.guardian.support import (
    approve_and_release,
    entails,
    make_committed_world,
    propose_follow_up,
    rows,
    scalar,
)
from tests.guardian.test_routes_approvals import key, ok, refused, sign_in


@pytest.fixture
def scene(api_client, engine, monkeypatch, tmp_path):
    """A campaign with a model-call row, a state change, a scheduled follow-up and a blocked proposal."""
    entails(monkeypatch, tmp_path, times=30)
    w = make_committed_world(engine)
    with engine.begin() as conn:
        run = make_run(conn, w.user, w.campaign, "researcher")
        model = make_action(conn, run, kind="model_call", status="recorded", summary="Drafted the dossier.")
        make_action(conn, run, kind="tool_call", status="recorded", summary="Read one public page.")
    scheduled = propose_follow_up(w, step="follow_up:1")
    assert approve_and_release(w, scheduled) == "scheduled"
    sign_in(api_client, w.user)
    return type("S", (), {"w": w, "client": api_client, "engine": engine, "scheduled": scheduled, "model": model})()


def entries(scene, **params):
    return ok(scene.client.get("/v1/control/recorder", params={"campaign_id": str(scene.w.campaign), **params}))


def test_the_recorder_lists_the_campaigns_entries_in_order_with_the_fields_of_the_timeline(scene):
    data = entries(scene)
    ids = [e["id"] for e in data]
    assert scene.model in ids and scene.scheduled.action_id in ids
    assert [(e["created_at"], e["run_id"], e["seq"]) for e in data] == sorted(
        (e["created_at"], e["run_id"], e["seq"]) for e in data
    )
    first = next(e for e in data if e["id"] == scene.model)
    assert set(first) >= {
        "id", "run_id", "seq", "created_at", "agent_role", "kind", "action_type", "status", "summary",
        "outcome_code", "cost_usd", "evidence_count",
    }  # fmt: skip
    assert (first["agent_role"], first["kind"], first["status"], first["cost_usd"]) == (
        "researcher",
        "model_call",
        "recorded",
        "0.000000",
    )
    sent = next(e for e in data if e["id"] == scene.scheduled.action_id)
    assert (sent["kind"], sent["action_type"], sent["status"]) == ("outside_action", "send_follow_up", "scheduled")
    assert {c["check"] for c in sent["checks"]} == {
        "citations",
        "consent",
        "scope",
        "budget",
        "injection",
        "rate_limit",
    }
    assert sent["approval"] == "approved"


def test_the_recorder_paginates_with_next_cursor(scene):
    everything = entries(scene)
    assert len(everything) >= 3
    response = scene.client.get("/v1/control/recorder", params={"campaign_id": str(scene.w.campaign), "limit": 2})
    page = ok(response)
    meta = response.json()["meta"]
    assert len(page) == 2 and meta["next_cursor"] and meta["limit"] == 2
    rest = ok(
        scene.client.get(
            "/v1/control/recorder",
            params={"campaign_id": str(scene.w.campaign), "limit": 2, "cursor": meta["next_cursor"]},
        )
    )
    assert [e["id"] for e in page + rest][: len(everything)] == [e["id"] for e in everything]
    last = scene.client.get(
        "/v1/control/recorder",
        params={"campaign_id": str(scene.w.campaign), "limit": len(everything)},
    )
    assert last.json()["meta"]["next_cursor"] is None


def test_the_recorder_reads_by_run_too(scene):
    run = scalar(scene.engine, "SELECT agent_run_id FROM agent_actions WHERE id = :i", i=scene.model)
    data = ok(scene.client.get("/v1/control/recorder", params={"run_id": str(run)}))
    assert scene.model in [e["id"] for e in data] and len(data) == 2


def test_exactly_one_of_campaign_id_and_run_id_is_required(scene):
    refused(scene.client.get("/v1/control/recorder"), 422, "validation_failed")
    both = {"campaign_id": str(scene.w.campaign), "run_id": str(uuid4())}
    refused(scene.client.get("/v1/control/recorder", params=both), 422, "validation_failed")


def test_an_unknown_campaign_or_run_and_another_persons_are_404(scene):
    refused(scene.client.get("/v1/control/recorder", params={"campaign_id": str(uuid4())}), 404, "not_found")
    refused(scene.client.get("/v1/control/recorder", params={"run_id": str(uuid4())}), 404, "not_found")
    stranger = make_committed_world(scene.engine)
    sign_in(scene.client, stranger.user)
    refused(scene.client.get("/v1/control/recorder", params={"campaign_id": str(scene.w.campaign)}), 404, "not_found")
    refused(scene.client.get(f"/v1/control/recorder/{scene.scheduled.action_id}"), 404, "not_found")


def test_one_entry_comes_back_in_full(scene):
    data = ok(scene.client.get(f"/v1/control/recorder/{scene.scheduled.action_id}"))
    assert data["id"] == scene.scheduled.action_id and data["kind"] == "outside_action"
    assert data["agent_role"] == "chaser" and data["policy_decision"] == "ask" and data["policy_version"]
    assert data["payload"]["to"] and data["payload"]["subject"]
    assert data["inputs"]["provenance"]["sentences"] and data["evidence"] is not None
    assert data["approval"]["status"] == "approved" and data["undo_until"] is not None
    reasons = {c["check"]: c["reason_code"] for c in data["checks"]}
    assert set(reasons) == {"citations", "consent", "scope", "budget", "injection", "rate_limit"}
    assert all(c["passed"] and c["guardian_version"] for c in data["checks"])
    assert data["cost_usd"] == "0.000000" and "cost_microusd" not in data and data["model_calls"] == []


def test_an_entry_shows_a_blocked_attempts_checks_with_their_reasons(scene):
    with scene.engine.begin() as conn:
        conn.execute(text("UPDATE campaigns SET follow_up_email = NULL WHERE id = :c"), {"c": scene.w.campaign})
    blocked = propose_follow_up(scene.w, step="follow_up:2")
    data = ok(scene.client.get(f"/v1/control/recorder/{blocked.action_id}"))
    assert data["status"] == "blocked" and data["outcome_code"]
    failed = [c for c in data["checks"] if not c["passed"]]
    assert failed and all(c["reason_code"] for c in failed)


def test_reading_an_entry_calls_no_model(scene, monkeypatch):
    from app.platform import llm

    def refuse(*args, **kwargs):
        raise AssertionError("a recorder read called a model")

    monkeypatch.setattr(llm, "complete", refuse, raising=False)
    ok(scene.client.get(f"/v1/control/recorder/{scene.scheduled.action_id}"))
    ok(scene.client.get(f"/v1/control/recorder/{scene.model}"))
    refused(scene.client.get("/v1/control/recorder/999999999"), 404, "not_found")
    refused(scene.client.get("/v1/control/recorder/0"), 404, "not_found")


# ---- undo -----------------------------------------------------------------------------------------------------------


def undo(scene, action_id=None, headers=None):
    return scene.client.post(
        f"/v1/control/actions/{action_id or scene.scheduled.action_id}/undo", headers=headers or key()
    )


def test_undo_inside_the_window_cancels_the_action_and_the_message(scene):
    data = ok(undo(scene))
    assert data == {"action": {"id": scene.scheduled.action_id, "status": "cancelled"}, "outbox_cancelled": 1}
    action = rows(
        scene.engine, "SELECT status::text, outcome_code FROM agent_actions WHERE id = :i", i=scene.scheduled.action_id
    )
    assert tuple(action[0]) == ("cancelled", "undone")
    assert (
        scalar(scene.engine, "SELECT status::text FROM outbox WHERE agent_action_id = :i", i=scene.scheduled.action_id)
        == "cancelled"
    )
    assert (
        scalar(
            scene.engine,
            "SELECT count(*) FROM queue_jobs WHERE unique_key LIKE :k",
            k=f"%a{scene.scheduled.action_id}.cancelled",
        )
        == 1
    )


def test_a_retry_with_the_same_key_returns_the_first_answer(scene):
    headers = key()
    first = undo(scene, headers=headers)
    again = undo(scene, headers=headers)
    assert again.json()["data"] == first.json()["data"] and again.json()["meta"]["idempotent_replay"] is True


@pytest.mark.parametrize(("message", "state"), [("sending", "sending"), ("sent", "sent")])
def test_after_the_relay_claimed_the_message_undo_is_409_with_the_state(scene, message, state):
    with scene.engine.begin() as c:
        c.execute(
            text(
                "UPDATE outbox SET status = CAST(:s AS outbox_status), not_before = now() - interval '1 minute', "
                "sent_at = CASE WHEN :s = 'sent' THEN now() END WHERE agent_action_id = :i"
            ),
            {"s": message, "i": scene.scheduled.action_id},
        )
    error = refused(undo(scene), 409, "conflict")
    assert error["details"]["state"] == state
    assert (
        scalar(scene.engine, "SELECT status::text FROM agent_actions WHERE id = :i", i=scene.scheduled.action_id)
        == "scheduled"
    )


def test_undo_of_something_that_is_not_a_scheduled_follow_up_is_409_and_of_another_persons_is_404(scene):
    error = refused(undo(scene, scene.model), 409, "conflict")
    assert error["details"]["state"] == "recorded"
    stranger = make_committed_world(scene.engine)
    sign_in(scene.client, stranger.user)
    refused(undo(scene), 404, "not_found")
    refused(undo(scene, 999999999), 404, "not_found")


def test_undo_needs_an_idempotency_key(scene):
    refused(scene.client.post(f"/v1/control/actions/{scene.scheduled.action_id}/undo"), 400, "idempotency_key_missing")


def test_recorder_reads_and_undo_need_only_the_api_login(scene, monkeypatch):
    current = config.get()
    unreachable = "postgresql://nobody:nothing@127.0.0.1:1/app_test"
    monkeypatch.setattr(
        config, "get", lambda: replace(current, database_url_worker=unreachable, database_url_match=unreachable)
    )
    monkeypatch.setattr(db, "_engines", {})
    try:
        entries(scene)
        ok(scene.client.get(f"/v1/control/recorder/{scene.scheduled.action_id}"))
        ok(undo(scene))
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()
