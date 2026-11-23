"""Invariant 4: nothing about a candidate reaches an employer before the candidate accepts (plan 06-04).

One role has an asked candidate and an accepted one. As a member of the organization every employer route is read and
every response body is searched for what identifies the asked candidate: their id, name, email, the introduction id and
every string of their frozen preview. The accepted candidate appears with exactly share_preview.on_accept and no email.
The test fails if an employer policy for asked rows, a shortlist entry without an acceptance, or an email before a
yes is ever introduced. A member of another organization gets 404 or an empty list from every route.
"""

import json

from sqlalchemy import text

from tests.hiring.test_introduction_routes import act, another_ask, key, make_stranger, rows, scene

MARKER = "ASKED-ONLY-QUOTE-7731"


def strings(value):
    """Every string inside a JSON-like value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def world_of_two(engine):
    s = scene(engine, accepted=True)
    asked, ask = another_ask(engine, s, name="Demo Asked Only")
    with engine.begin() as c:  # make the asked candidate's preview recognisable (content is frozen outside maintenance)
        c.execute(text("SET LOCAL app.maintenance = 'on'"))
        c.execute(
            text(
                "UPDATE introductions SET share_preview = "
                "jsonb_set(share_preview, '{on_accept,facts,0,statement}', to_jsonb(CAST(:m AS text))) WHERE id = :i"
            ),
            {"m": MARKER, "i": ask.id},
        )
    with engine.begin() as c:  # the asked candidate has already sent a signal: it must stay unseen too
        c.execute(
            text(
                """INSERT INTO interest_signals
                     (sender_side, user_id, organization_id, role_id, sent_by, week_start, slot)
                   VALUES ('candidate', :u, :o, :r, :u, date_trunc('week', now() AT TIME ZONE 'UTC')::date, 1)"""
            ),
            {"u": asked.id, "o": s.org.id, "r": s.role.id},
        )
    return s, asked, ask


def employer_reads(client, s, ask):
    """(path, response) for every employer route of this plan."""
    org, role = s.org.id, s.role.id
    return [
        (path, client.get(path))
        for path in (
            f"/v1/introductions?role_id={role}",
            f"/v1/introductions?org_id={org}",
            f"/v1/introductions?org_id={org}&status=asked",
            f"/v1/introductions/{ask.id}",
            f"/v1/introductions/{s.ask.id}",
            f"/v1/roles/{role}/shortlist",
            f"/v1/orgs/{org}/answer-clock",
            f"/v1/signals?org_id={org}",
        )
    ]


def test_no_employer_route_says_anything_about_the_asked_candidate(engine, api_client):
    s, asked, ask = world_of_two(engine)
    act(api_client, s.member)
    secrets = {str(asked.id), asked.name, asked.email, str(ask.id), MARKER, "Demo Asked Only"}
    secrets |= {x for x in strings(ask.preview) if len(x) > 8 and x not in set(strings(s.ask.preview))}

    for path, response in employer_reads(api_client, s, ask):
        body = json.dumps(response.json())
        for secret in secrets:
            assert secret not in body, f"{path} leaked {secret!r}"
    assert api_client.get(f"/v1/introductions/{ask.id}").status_code == 404
    for verb, path, payload in (
        ("post", f"/v1/introductions/{ask.id}/answer", {"answer": "yes", "reason": "x"}),
        ("post", "/v1/signals", {"introduction_id": str(ask.id)}),
    ):
        assert getattr(api_client, verb)(path, json=payload, headers=key()).status_code == 404, path


def test_the_accepted_candidate_appears_with_exactly_what_was_shared_and_no_email(engine, api_client):
    s, asked, ask = world_of_two(engine)
    act(api_client, s.member)
    responses = dict(employer_reads(api_client, s, ask))
    org, role = s.org.id, s.role.id

    shortlist = responses[f"/v1/roles/{role}/shortlist"].json()["data"]
    assert [(e["introduction_id"], e["shared"], e["email"]) for e in shortlist] == [
        (str(s.ask.id), s.ask.preview["on_accept"], None)
    ]
    assert [i["id"] for i in responses[f"/v1/introductions?role_id={role}"].json()["data"]] == [str(s.ask.id)]
    assert [i["id"] for i in responses[f"/v1/introductions?org_id={org}"].json()["data"]] == [str(s.ask.id)]
    assert responses[f"/v1/introductions?org_id={org}&status=asked"].json()["data"] == []
    assert responses[f"/v1/introductions/{s.ask.id}"].json()["data"]["shared"] == s.ask.preview["on_accept"]
    assert [o["introduction_id"] for o in responses[f"/v1/orgs/{org}/answer-clock"].json()["data"]["owed"]] == [
        str(s.ask.id)
    ]
    for response in responses.values():  # the account email is nowhere, before a yes
        assert s.candidate.email not in json.dumps(response.json())
        assert "on_employer_yes" not in json.dumps(response.json())


def test_the_database_has_no_employer_door_to_an_asked_row(engine, api_client):
    """The routes above are fenced by row-level security; this reads the tables themselves as the employer's member."""
    s, asked, ask = world_of_two(engine)
    with engine.begin() as c:
        c.execute(text("SET LOCAL ROLE app_api"))
        c.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(s.member)})
        seen = c.execute(text("SELECT user_id, status::text FROM introductions")).all()
        shortlist = c.execute(text("SELECT user_id FROM shortlist_entries")).all()
        signals = c.execute(text("SELECT user_id FROM interest_signals")).all()
    assert [tuple(r) for r in seen] == [(s.candidate.id, "accepted")]
    assert [r[0] for r in shortlist] == [s.candidate.id]
    assert [r[0] for r in signals] == []  # the asked candidate's signal is hidden until they accept
    assert rows(engine, "SELECT count(*) FROM shortlist_entries WHERE user_id = :u", u=asked.id) == [(0,)]


def test_a_member_of_another_organization_gets_nothing_anywhere(engine, api_client):
    s, asked, ask = world_of_two(engine)
    other = scene(engine)
    act(api_client, other.member)
    for path, response in employer_reads(api_client, s, ask):
        if response.status_code == 200:
            assert response.json()["data"] in ([], None), path
        else:
            assert response.status_code == 404, path
    act(api_client, make_stranger(engine))
    for path, response in employer_reads(api_client, s, ask):
        assert response.status_code == 404 or response.json()["data"] == [], path
