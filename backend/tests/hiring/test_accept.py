"""HIR-04 and HIR-05: accepting an ask is one transaction that releases exactly the preview the candidate saw (06-04).

The accept writes the introduction consent, moves the status (the guard sets the due time), stores the shortlist entry,
queues one notice for the organization and writes one audit row. A refusal anywhere leaves none of them.
"""

from tests.hiring.test_introduction_routes import VERSION, accept_body, act, key, rows, scene

NOTICE = "SELECT count(*) FROM outbox WHERE template = 'introduction_accepted_v1'"
CONSENT = "SELECT count(*) FROM consents WHERE purpose = 'introduction'"
AUDIT = "SELECT count(*) FROM audit_log WHERE event_type = 'hiring.introduction_accepted'"
SHORTLIST = "SELECT count(*) FROM shortlist_entries"


def nothing_shared(engine) -> None:
    assert rows(engine, "SELECT status::text, responded_at, answer_due_at FROM introductions") == [
        ("asked", None, None)
    ]
    for sql in (CONSENT, SHORTLIST, NOTICE, AUDIT):
        assert rows(engine, sql) == [(0,)], sql


def test_accepting_does_everything_in_one_step(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)

    response = api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key())

    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == {"introduction", "shared"}  # no campaign: Phase 7 starts one
    intro = data["introduction"]
    assert intro["id"] == str(s.ask.id) and intro["status"] == "accepted"
    assert intro["responded_at"] is not None and intro["answer_due_at"] is not None
    assert s.org.name in data["shared"] and "only if they answer yes" in data["shared"]
    # the answer clock: five business days on in the organization's time zone
    assert rows(
        engine,
        "SELECT add_business_days(responded_at, 5, 'UTC') = answer_due_at, status::text FROM introductions",
    ) == [(True, "accepted")]
    consent = rows(
        engine,
        "SELECT organization_id, role_id, text_version, action::text FROM consents WHERE purpose = 'introduction'",
    )
    assert [tuple(c) for c in consent] == [(s.org.id, s.role.id, VERSION, "granted")]
    entry = rows(engine, "SELECT shared_payload, contact_released_at, user_id FROM shortlist_entries")
    assert len(entry) == 1 and entry[0].shared_payload == s.ask.preview and entry[0].user_id == s.candidate.id
    assert entry[0].contact_released_at is not None
    notice = rows(engine, "SELECT dedupe_key, user_id, payload FROM outbox WHERE template = 'introduction_accepted_v1'")
    assert len(notice) == 1 and notice[0].user_id is None
    assert notice[0].dedupe_key == f"introduction_accepted:{s.ask.id}"
    assert notice[0].payload == {"introduction_id": str(s.ask.id)}  # ids only: no name, no email
    audit = rows(
        engine,
        "SELECT organization_id, subject_user_id, actor_user_id, details FROM audit_log "
        "WHERE event_type = 'hiring.introduction_accepted'",
    )
    assert len(audit) == 1 and audit[0].organization_id == s.org.id and audit[0].actor_user_id == s.candidate.id
    assert set(audit[0].details) == {"introduction_id", "role_id"}


def test_the_due_time_counts_in_the_organizations_time_zone(engine, api_client):
    s = scene(engine, timezone_name="Asia/Kolkata")
    act(api_client, s.candidate.id)
    assert (
        api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key()).status_code
        == 200
    )
    assert rows(
        engine, "SELECT add_business_days(responded_at, 5, 'Asia/Kolkata') = answer_due_at FROM introductions"
    ) == [(True,)]


def test_a_wrong_hash_shares_nothing(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    body = {**accept_body(s.ask), "share_preview_hash": "0" * 64}

    refused = api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=body, headers=key())

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "preview_changed"
    nothing_shared(engine)


def test_a_stale_wording_shares_nothing(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    body = {**accept_body(s.ask), "consent_text_version": "introduction-2026-10"}

    refused = api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=body, headers=key())

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "conflict"
    assert refused.json()["error"]["details"]["state"] == "consent_text_changed"
    nothing_shared(engine)


def test_a_failure_after_the_consent_rolls_the_consent_back_too(engine, api_client, monkeypatch):
    s = scene(engine)
    act(api_client, s.candidate.id)
    from app.hiring import introductions

    def boom(*args, **kwargs):
        raise RuntimeError("the outbox is down")

    monkeypatch.setattr(introductions.outbox, "add", boom)
    api_client.raise_server_exceptions = False
    assert (
        api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key()).status_code
        == 500
    )
    nothing_shared(engine)


def test_a_body_with_a_missing_or_extra_field_is_422(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    path = f"/v1/introductions/{s.ask.id}/accept"
    assert api_client.post(path, json={"share_preview_hash": "x"}, headers=key()).status_code == 422
    assert api_client.post(path, json={**accept_body(s.ask), "twin": True}, headers=key()).status_code == 422
    nothing_shared(engine)


def test_there_is_no_way_back_and_no_second_accept(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    assert (
        api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key()).status_code
        == 200
    )

    again = api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key())
    decline = api_client.post(f"/v1/introductions/{s.ask.id}/decline", headers=key())

    for refused in (again, decline):
        assert refused.status_code == 409 and refused.json()["error"]["code"] == "already_resolved"
        assert refused.json()["error"]["details"] == {"resolution": "accepted"}
    assert rows(engine, "SELECT status::text FROM introductions") == [("accepted",)]


def test_the_employer_sees_the_candidate_only_after_this_step(engine, api_client):
    s = scene(engine)
    act(api_client, s.member)
    assert api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()["data"] == []
    act(api_client, s.candidate.id)
    api_client.post(f"/v1/introductions/{s.ask.id}/accept", json=accept_body(s.ask), headers=key())
    act(api_client, s.member)
    shortlist = api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()["data"]
    assert [e["shared"] for e in shortlist] == [s.ask.preview["on_accept"]]
