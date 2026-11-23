"""Invariant 6, responses: a repeated accept, decline or answer changes nothing (plan 06-04).

The same Idempotency-Key replays the first response; a new key meets the status rules (409 already_resolved). Either way
there is still one consent, one shortlist entry, one notice and one audit row.
"""

from tests.hiring.test_introduction_routes import accept_body, act, key, rows, scene


def counts(engine) -> dict[str, int]:
    out = {}
    for name, sql in {
        "consent": "SELECT count(*) FROM consents WHERE purpose = 'introduction'",
        "shortlist": "SELECT count(*) FROM shortlist_entries",
        "notice": "SELECT count(*) FROM outbox WHERE template = 'introduction_accepted_v1'",
        "audit": "SELECT count(*) FROM audit_log WHERE event_type = 'hiring.introduction_accepted'",
    }.items():
        out[name] = rows(engine, sql)[0][0]
    return out


def test_the_same_accept_twice_replays_and_a_second_key_is_already_resolved(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    headers = key()
    path = f"/v1/introductions/{s.ask.id}/accept"

    first = api_client.post(path, json=accept_body(s.ask), headers=headers)
    replay = api_client.post(path, json=accept_body(s.ask), headers=headers)
    other_key = api_client.post(path, json=accept_body(s.ask), headers=key())

    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json()["data"] == first.json()["data"]
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert other_key.status_code == 409 and other_key.json()["error"]["code"] == "already_resolved"
    assert counts(engine) == {"consent": 1, "shortlist": 1, "notice": 1, "audit": 1}


def test_the_same_decline_twice_changes_nothing_more(engine, api_client):
    s = scene(engine)
    act(api_client, s.candidate.id)
    headers = key()
    path = f"/v1/introductions/{s.ask.id}/decline"
    first = api_client.post(path, headers=headers)
    replay = api_client.post(path, headers=headers)
    again = api_client.post(path, headers=key())
    assert (first.status_code, replay.status_code, again.status_code) == (200, 200, 409)
    assert again.json()["error"]["details"] == {"resolution": "declined"}
    assert rows(engine, "SELECT count(*) FROM audit_log WHERE event_type = 'hiring.introduction_declined'") == [(1,)]


def test_the_same_answer_twice_sends_one_email_and_writes_one_row(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    headers = key()
    path = f"/v1/introductions/{s.ask.id}/answer"
    body = {"answer": "yes", "reason": "Strong fit"}
    first = api_client.post(path, json=body, headers=headers)
    replay = api_client.post(path, json=body, headers=headers)
    again = api_client.post(path, json={"answer": "no", "reason": "Changed my mind"}, headers=key())
    assert (first.status_code, replay.status_code, again.status_code) == (200, 200, 409)
    assert replay.json()["data"] == first.json()["data"]
    assert rows(engine, "SELECT count(*) FROM outbox WHERE template = 'introduction_answered_v1'") == [(1,)]
    assert rows(engine, "SELECT count(*) FROM audit_log WHERE event_type = 'hiring.introduction_answered'") == [(1,)]
    assert rows(engine, "SELECT employer_answer::text, employer_answer_reason FROM introductions") == [
        ("yes", "Strong fit")
    ]
