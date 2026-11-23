"""HIR-05: GET /v1/roles/{id}/shortlist lists only candidates who said yes, with exactly what they shared (06-04)."""

from sqlalchemy import text

from tests.hiring.test_introduction_routes import act, another_ask, key, make_stranger, rows, scene


def test_only_accepted_candidates_appear_with_the_evidence_table_and_no_email(engine, api_client):
    s = scene(engine, accepted=True)
    _, asked = another_ask(engine, s)  # asked, not accepted: nowhere in the shortlist
    act(api_client, s.member)

    body = api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()

    assert [e["introduction_id"] for e in body["data"]] == [str(s.ask.id)]
    entry = body["data"][0]
    assert entry["shared"] == s.ask.preview["on_accept"]
    assert entry["shared"]["evidence"] and entry["shared"]["evidence"][0]["verdict"] == "met"
    assert entry["email"] is None and entry["status"] == "accepted" and entry["employer_answer"] is None
    assert entry["is_overdue"] is False and entry["answer_due_at"] and entry["responded_at"]
    assert entry["signals"] == {"from_candidate": False, "from_you": False}
    assert s.candidate.email not in str(body) and str(asked.id) not in str(body)
    assert body["meta"]["limit"] == 20 and body["meta"]["next_cursor"] is None


def test_the_email_appears_only_after_a_yes(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.member)
    api_client.post(
        f"/v1/introductions/{s.ask.id}/answer", json={"answer": "no", "reason": "Skills gap"}, headers=key()
    )
    entry = api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()["data"][0]
    assert entry["employer_answer"] == "no" and entry["email"] is None

    t = scene(engine, accepted=True)
    act(api_client, t.member)
    api_client.post(f"/v1/introductions/{t.ask.id}/answer", json={"answer": "yes", "reason": "Fit"}, headers=key())
    entry = api_client.get(f"/v1/roles/{t.role.id}/shortlist").json()["data"][0]
    assert entry["employer_answer"] == "yes" and entry["email"] == t.candidate.email


def test_newest_first_with_a_cursor_and_overdue_flag(engine, api_client):
    s = scene(engine, accepted=True)
    second, ask = another_ask(engine, s, accepted=True)
    with engine.begin() as c:
        c.execute(text("SET LOCAL app.maintenance = 'on'"))
        c.execute(
            text("UPDATE introductions SET answer_due_at = now() - interval '1 hour' WHERE id = :i"), {"i": s.ask.id}
        )
    act(api_client, s.member)

    first = api_client.get(f"/v1/roles/{s.role.id}/shortlist", params={"limit": 1}).json()
    assert [e["introduction_id"] for e in first["data"]] == [str(ask.id)]
    rest = api_client.get(
        f"/v1/roles/{s.role.id}/shortlist", params={"limit": 1, "cursor": first["meta"]["next_cursor"]}
    ).json()
    assert [(e["introduction_id"], e["is_overdue"]) for e in rest["data"]] == [(str(s.ask.id), True)]
    assert rest["meta"]["next_cursor"] is None


def test_signals_show_on_the_entry(engine, api_client):
    s = scene(engine, accepted=True)
    act(api_client, s.candidate.id)
    assert api_client.post("/v1/signals", json={"introduction_id": str(s.ask.id)}, headers=key()).status_code == 201
    act(api_client, s.member)
    assert api_client.post("/v1/signals", json={"introduction_id": str(s.ask.id)}, headers=key()).status_code == 201
    entry = api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()["data"][0]
    assert entry["signals"] == {"from_candidate": True, "from_you": True}


def test_another_organizations_or_an_unknown_role_is_404(engine, api_client):
    s = scene(engine, accepted=True)
    other = scene(engine)
    act(api_client, other.member)
    assert api_client.get(f"/v1/roles/{s.role.id}/shortlist").status_code == 404
    act(api_client, make_stranger(engine))
    assert api_client.get(f"/v1/roles/{s.role.id}/shortlist").status_code == 404
    act(api_client, s.member)
    assert api_client.get("/v1/roles/00000000-0000-4000-8000-000000000000/shortlist").status_code == 404
    assert rows(engine, "SELECT count(*) FROM shortlist_entries") == [(1,)]


def test_a_withdrawn_introduction_consent_removes_the_entry(engine, api_client):
    s = scene(engine, accepted=True)
    with engine.begin() as c:  # the candidate takes the consent back
        c.execute(
            text(
                """INSERT INTO consents (user_id, purpose, action, text_version, organization_id, role_id, withdraws_id)
                   SELECT user_id, purpose, 'withdrawn', text_version, organization_id, role_id, id
                   FROM consents WHERE purpose = 'introduction'"""
            )
        )
    act(api_client, s.member)
    assert api_client.get(f"/v1/roles/{s.role.id}/shortlist").json()["data"] == []
