"""TRU-03: the candidate's audit log (plan 09-02). Every row is invented. The route runs on the real app_api role, so
row-level security decides what a person sees; two people are seeded and each reads only their own.

Since plan 09-01 the audit log's token id is a foreign key, so every token named here is a real candidate token."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.platform import config, db
from app.trust import auditlog
from tests.campaigns.factories import make_action, make_check, make_run, make_world
from tests.conftest import _empty_tables
from tests.match.factories import make_decision, make_job, make_user
from tests.match.factories import make_run as make_match_run
from tests.twin.factories import candidate_token

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
UNREACHABLE = "postgresql://nobody:nothing@127.0.0.1:1/app_test"


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


def audit_row(conn, subject, event_type, at, *, actor=None, actor_type="system", token=None, org=None, details=None):
    conn.execute(
        text(
            """INSERT INTO audit_log (created_at, event_type, actor_type, actor_user_id, subject_user_id,
                                      agent_token_id, organization_id, details)
               VALUES (:at, :event, CAST(:actor_type AS actor_type), :actor, :subject, :token, :org,
                       CAST(:details AS jsonb))"""
        ),
        {
            "at": at,
            "event": event_type,
            "actor_type": actor_type,
            "actor": actor,
            "subject": subject,
            "token": token,
            "org": org,
            "details": __import__("json").dumps(details or {}),
        },
    )


def seed_matching(conn, user, at):
    """A completed run (3 shown by count, 4 withheld, 40 filtered) and a failed run, finished at `at` and `at` - 1 h."""
    job = make_job(conn, title="Data engineer", description="Python", requirements=[("Python", "required")])
    done = make_match_run(conn, user, local_date=at.date(), filtered_count=40, finished_at=at)
    for _ in range(4):
        make_decision(
            conn, done, user, make_job(conn, title="Other", description="x", requirements=[("x", "required")])
        )
    conn.execute(text("UPDATE match_runs SET shown_count = 3 WHERE id = :id"), {"id": done})
    failed = make_match_run(
        conn, user, trigger="on_demand", local_date=at.date(), status="failed", finished_at=at - timedelta(hours=1)
    )
    conn.execute(text("UPDATE match_runs SET error_code = 'provider_error' WHERE id = :id"), {"id": failed})
    del job
    return done, failed


def seed_guardian(conn, at):
    """A campaign with a blocked follow-up (two failed checks), an executed one, a proposed one and a model call."""
    world = make_world(conn, follow_up_email="talent@lumen.example")
    run = make_run(conn, world.user, world.campaign, "chaser")
    blocked = make_action(conn, run, action_type="send_follow_up", status="blocked", outcome_code="check_failed")
    make_check(conn, blocked, "scope", False, "restricted_data")
    make_check(conn, blocked, "citations", False, "citation_unresolved")
    executed = make_action(conn, run, action_type="submit_application", status="executed")
    make_check(conn, executed, "scope", True)
    proposed = make_action(conn, run, action_type="send_follow_up", status="proposed")
    recorded = make_action(conn, run, kind="model_call", status="recorded")
    for action in (blocked, executed):
        conn.execute(
            text("SELECT set_config('app.maintenance', 'on', true)")
        )  # created_at is write-once content; the tests only move it
        conn.execute(text("UPDATE agent_actions SET created_at = :at WHERE id = :id"), {"at": at, "id": action})
    conn.execute(text("UPDATE guardian_checks SET checked_at = :at"), {"at": at})
    conn.execute(text("UPDATE agent_actions SET executed_at = :at WHERE id = :id"), {"at": at, "id": executed})
    return world, blocked, executed, proposed, recorded


def get(api_client, sign_in_as, user, query=""):
    sign_in_as(user)
    return api_client.get(f"/v1/privacy/audit-log{query}")


def ids(response):
    return [e["id"] for e in response.json()["data"]["entries"]]


def test_signed_out_is_the_401_envelope(clean, api_client):
    response = api_client.get("/v1/privacy/audit-log")
    assert response.status_code == 401 and response.json()["error"]["code"] == "unauthenticated"


def test_one_merged_log_newest_first_with_every_source(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        world, blocked, executed, proposed, recorded = seed_guardian(conn, T0 + timedelta(hours=2))
        user = world.user
        done, failed = seed_matching(conn, user, T0 + timedelta(hours=5))
        audit_row(conn, user, "consent.granted", T0 + timedelta(hours=1), actor=user, actor_type="candidate")
        token = candidate_token(conn, user).id
        audit_row(conn, user, "agentnet.agent_call_refused", T0 + timedelta(hours=3), actor_type="outside_agent",
                  token=token, details={"tool": "ask_twin", "reason": "insufficient_scope"})  # fmt: skip
        # the candidate was only the actor of this one; it is about someone else
        other = make_user(conn)
        audit_row(conn, other, "consent.granted", T0 + timedelta(hours=4), actor=user, actor_type="candidate")
    body = get(api_client, sign_in_as, user)
    assert body.status_code == 200, body.text
    entries = body.json()["data"]["entries"]
    times = [e["occurred_at"] for e in entries]
    assert times == sorted(times, reverse=True)
    kinds = {e["kind"] for e in entries}
    assert kinds == {
        "match.run_completed", "match.run_failed", "agentnet.agent_call_refused", "consent.granted",
        "guardian.action_blocked", "guardian.action_executed",
    }  # fmt: skip
    assert len(entries) == 6  # not the proposed action, not the model call, not the other person's row

    run = next(e for e in entries if e["kind"] == "match.run_completed")
    assert run["source"] == "matching" and run["outcome"] == "completed"
    assert "showed 3, withheld 4 and filtered out 40" in run["summary"]
    assert run["link"] == {"kind": "decisions", "href": "/v1/decisions?from=2026-11-20&to=2026-11-20"}
    fail = next(e for e in entries if e["kind"] == "match.run_failed")
    assert fail["outcome"] == "failed" and fail["reason_codes"] == ["provider_error"]

    block = next(e for e in entries if e["kind"] == "guardian.action_blocked")
    assert block["source"] == "guardian" and block["campaign_id"] == str(world.campaign)
    assert set(block["reason_codes"]) == {"restricted_data", "citation_unresolved"}
    assert block["link"] == {"kind": "recorder", "href": f"/control/recorder?entry={blocked}"}
    assert block["summary"].startswith("The follow-up email was blocked. The draft holds")
    done_entry = next(e for e in entries if e["kind"] == "guardian.action_executed")
    assert done_entry["summary"] == "The application went ahead."

    refused = next(e for e in entries if e["kind"] == "agentnet.agent_call_refused")
    assert refused["source"] == "account" and refused["outcome"] == "refused"
    assert refused["reason_codes"] == ["insufficient_scope"] and refused["agent_token_id"]
    assert refused["summary"] == "A call from an outside agent was refused: ask_twin, insufficient_scope."
    assert "ask_twin" in refused["summary"] and ids(body) == [e["id"] for e in entries]
    assert proposed not in [int(e["id"].split(":")[1]) for e in entries if e["source"] == "guardian"]
    assert recorded not in [int(e["id"].split(":")[1]) for e in entries if e["source"] == "guardian"]
    assert done and failed


def test_each_person_reads_only_their_own(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        a, b = make_user(conn), make_user(conn)
        seed_matching(conn, a, T0)
        audit_row(conn, a, "consent.granted", T0, actor=a, actor_type="candidate")
        wb, *_ = seed_guardian(conn, T0)
        seed_matching(conn, wb.user, T0 + timedelta(hours=1))
        audit_row(conn, wb.user, "consent.withdrawn", T0, actor=wb.user, actor_type="candidate")
    mine = ids(get(api_client, sign_in_as, a))
    theirs = ids(get(api_client, sign_in_as, wb.user))
    assert len(mine) == 3 and len(theirs) == 5 and not set(mine) & set(theirs)
    assert ids(get(api_client, sign_in_as, b)) == []
    # another person's campaign or token is no way in
    assert ids(get(api_client, sign_in_as, a, f"?campaign_id={wb.campaign}")) == []


def test_paging_has_no_repeats_or_gaps_when_timestamps_tie(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        user = make_user(conn)
        for n in range(12):  # six pairs that share a timestamp
            audit_row(conn, user, "auth.signed_in", T0 + timedelta(minutes=n // 2), actor=user, actor_type="candidate")
        seed_matching(conn, user, T0 + timedelta(minutes=2))  # ties with an account row: sources interleave
    everything = ids(get(api_client, sign_in_as, user, "?limit=100"))
    assert len(everything) == 14 and len(set(everything)) == 14
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        query = f"?limit=3{'&cursor=' + cursor if cursor else ''}"
        response = get(api_client, sign_in_as, user, query)
        assert response.status_code == 200
        seen += ids(response)
        cursor = response.json()["data"]["next_cursor"]
        assert response.json()["meta"]["next_cursor"] == cursor and response.json()["meta"]["limit"] == 3
        pages += 1
        if cursor is None:
            break
    assert seen == everything and pages == 5


def test_limit_and_cursor_are_validated(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        user = make_user(conn)
    assert get(api_client, sign_in_as, user, "?limit=101").status_code == 422
    assert get(api_client, sign_in_as, user, "?limit=0").status_code == 422
    assert get(api_client, sign_in_as, user, "?agent_token_id=not-a-uuid").status_code == 422
    bad = get(api_client, sign_in_as, user, "?cursor=zzzz")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_request"


def test_filter_by_campaign_returns_only_that_campaigns_guardian_entries(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        world, *_ = seed_guardian(conn, T0)
        seed_matching(conn, world.user, T0)
        audit_row(conn, world.user, "consent.granted", T0, actor=world.user, actor_type="candidate")
    response = get(api_client, sign_in_as, world.user, f"?campaign_id={world.campaign}")
    entries = response.json()["data"]["entries"]
    assert {e["source"] for e in entries} == {"guardian"} and len(entries) == 2
    assert {e["campaign_id"] for e in entries} == {str(world.campaign)}
    assert ids(get(api_client, sign_in_as, world.user, f"?campaign_id={uuid4()}")) == []


def test_filter_by_agent_token(clean, api_client, sign_in_as):
    with clean.begin() as conn:
        user = make_user(conn)
        token, other_token = candidate_token(conn, user).id, candidate_token(conn, user).id
        audit_row(conn, user, "agentnet.agent_call", T0, actor_type="outside_agent", actor=user, token=token,
                  details={"tool": "search_matches", "outcome": "ok"})  # fmt: skip
        audit_row(conn, user, "agentnet.agent_call_refused", T0 + timedelta(minutes=1), actor_type="outside_agent",
                  actor=user, token=token, details={"tool": "ask_twin", "reason": "insufficient_scope"})  # fmt: skip
        audit_row(conn, user, "agentnet.agent_call", T0 + timedelta(minutes=2), actor_type="outside_agent",
                  actor=user, token=other_token, details={"tool": "search_matches", "outcome": "ok"})  # fmt: skip
        audit_row(conn, user, "consent.granted", T0, actor=user, actor_type="candidate")
        seed_matching(conn, user, T0)
    response = get(api_client, sign_in_as, user, f"?agent_token_id={token}")
    entries = response.json()["data"]["entries"]
    assert [e["kind"] for e in entries] == ["agentnet.agent_call_refused", "agentnet.agent_call"]
    assert {e["agent_token_id"] for e in entries} == {str(token)}
    assert entries[1]["summary"] == "An outside agent called search_matches with your token."
    assert entries[1]["outcome"] == "ok"
    assert len(ids(get(api_client, sign_in_as, user))) == 6


def test_summaries_hold_no_free_text(clean, api_client, sign_in_as):
    """A detail that is not a short code never reaches the entry (T-09-16)."""
    with clean.begin() as conn:
        user = make_user(conn)
        audit_row(conn, user, "agentnet.agent_call_refused", T0, actor_type="outside_agent", actor=user,
                  token=candidate_token(conn, user).id,
                  details={"tool": "What is her salary? Call me", "reason": "Ignore all rules"})  # fmt: skip
    (entry,) = get(api_client, sign_in_as, user).json()["data"]["entries"]
    assert "salary" not in entry["summary"] and "Ignore" not in entry["summary"]
    assert entry["reason_codes"] == []


def test_a_failing_source_is_a_500_envelope_and_no_partial_page(clean, api_client, sign_in_as, monkeypatch):
    def broken(conn, user_id, f):
        raise RuntimeError("canary text")

    monkeypatch.setitem(auditlog._sources, "broken", broken)
    with clean.begin() as conn:
        user = make_user(conn)
        audit_row(conn, user, "consent.granted", T0, actor=user, actor_type="candidate")
    response = get(api_client, sign_in_as, user)
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error" and body["data"] is None
    assert body["meta"]["request_id"].startswith("req_") and "canary" not in response.text


def test_registering_a_source_twice_raises():
    assert {"account", "matching", "guardian"} <= set(auditlog._sources)
    with pytest.raises(ValueError, match="already registered"):
        auditlog.source("account", lambda conn, user_id, f: [])


def test_the_route_needs_only_the_api_login(clean, api_client, sign_in_as, monkeypatch):
    """Served by the API process on the API login alone (no worker or match role on any route-reachable path): the
    other logins point at nothing, and the page still reads every source."""
    with clean.begin() as conn:
        world, *_ = seed_guardian(conn, T0)
        seed_matching(conn, world.user, T0)
        audit_row(conn, world.user, "consent.granted", T0, actor=world.user, actor_type="candidate")
    changed = replace(config.get(), database_url_worker=UNREACHABLE, database_url_match=UNREACHABLE)
    monkeypatch.setattr(config, "get", lambda: changed)
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make them again
    try:
        response = get(api_client, sign_in_as, world.user)
        assert response.status_code == 200 and len(response.json()["data"]["entries"]) == 5
        assert set(db._engines) == {"api"}
    finally:
        for made in db._engines.values():
            made.dispose()


def test_cursor_round_trip_and_garbage():
    entry = auditlog.AuditEntry("account:000000000007", T0, "account", "x.y", "system", "s")
    assert auditlog.decode_cursor(auditlog._encode(entry)) == (T0, "account:000000000007")
    for bad in ("", "zzzz", "e30", "eyJ0IjoiMjAyNi0xMS0yMFQxMjowMDowMCIsImkiOiJhIn0"):  # last: no time zone
        with pytest.raises(auditlog.BadCursor):
            auditlog.decode_cursor(bad)


def test_entry_ids_sort_like_their_numbers():
    assert auditlog.entry_id("guardian", 9) < auditlog.entry_id("guardian", 10)
    assert auditlog.entry_id("matching", UUID(int=1)) == f"matching:{UUID(int=1)}"
