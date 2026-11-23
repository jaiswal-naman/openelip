"""HIR-03: GET /v1/roles/{id}/preview shows counts only (plan 06-03). A count of 1 to 4 reads "fewer than 5", and the
answer holds no identifier of any person (T-06-16).

The counts come from the database function role_preview over rows seeded with the 06-02 factories: a finished role run
with introduced and withheld decisions, and introductions on top of it. The ask pipeline is not run. Invented data.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.platform.sessions import Principal, current_user
from tests.hiring.factories import (
    make_accepted,
    make_ask,
    make_candidate,
    make_introduced_decision,
    make_open_role,
    make_org,
    make_role,
)


@pytest.fixture
def desk(engine, api_client):
    d = SimpleNamespace(client=api_client)
    with engine.begin() as conn:
        d.org, d.other = make_org(conn), make_org(conn, "Brightwell Demo")
        d.role = make_open_role(conn, d.org)
        d.draft = make_role(conn, d.org)
        people = [make_candidate(conn, name=f"Demo Person{n}") for n in range(8)]
        first = make_introduced_decision(conn, d.role, people[0])
        introduced = [first] + [make_introduced_decision(conn, d.role, who, run=first.run) for who in people[1:6]]
        for who in people[6:]:  # two withheld in the same run
            make_introduced_decision(conn, d.role, who, run=first.run, outcome="withheld")
        # three asks waiting and one accepted: waiting is 3 ("fewer than 5"), said_yes is exactly 1
        for decision, who in zip(introduced[1:4], people[1:4], strict=True):
            make_ask(conn, d.role, who, decision=decision)
        make_accepted(conn, d.role, people[4], ask=make_ask(conn, d.role, people[4], decision=introduced[4]))
        d.people = people
        d.names = [who.name for who in people]
        d.emails = [who.email for who in people]
        d.ids = [str(who.id) for who in people]
    d.sign_in = lambda user: api_client.app.dependency_overrides.__setitem__(current_user, lambda: Principal(user))
    d.sign_in(d.org.owner)
    return d


def test_the_preview_is_counts_only_with_one_to_four_shown_as_fewer_than_five(desk):
    response = desk.client.get(f"/v1/roles/{desk.role.id}/preview")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["role_id"] == str(desk.role.id)
    assert data["eligible_candidates"] == 8  # six introduced and two withheld passed the hard filter
    assert data["selected_to_ask"] == 6
    assert data["waiting_for_reply"] == "fewer than 5"  # three are waiting
    assert data["said_yes"] == 1  # exact: each is already on the shortlist
    assert data["said_no_or_expired"] == "fewer than 5"  # none is hidden too: 0 would show an ask that moved
    assert data["suppressed_below"] == 5 and "Counts only" in data["note"]
    assert data["as_of"] is not None
    assert set(data) == {
        "role_id",
        "as_of",
        "eligible_candidates",
        "selected_to_ask",
        "waiting_for_reply",
        "said_yes",
        "said_no_or_expired",
        "suppressed_below",
        "note",
    }


def test_the_answer_holds_no_user_id_name_or_email_of_any_candidate(desk):
    text = json.dumps(desk.client.get(f"/v1/roles/{desk.role.id}/preview").json())

    assert len(desk.names) == len(desk.emails) == len(desk.ids) == 8
    for secret in [*desk.names, *desk.emails, *desk.ids]:
        assert secret not in text


def test_a_draft_has_no_counts_and_another_organization_gets_not_found(desk):
    draft = desk.client.get(f"/v1/roles/{desk.draft.id}/preview").json()["data"]
    assert (draft["eligible_candidates"], draft["said_yes"], draft["as_of"]) == (0, 0, None)

    desk.sign_in(desk.other.owner)
    response = desk.client.get(f"/v1/roles/{desk.role.id}/preview")
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert desk.client.get(f"/v1/roles/{uuid4()}/preview").status_code == 404


def test_signed_out_is_unauthenticated(desk):
    desk.client.app.dependency_overrides.clear()

    assert desk.client.get(f"/v1/roles/{desk.role.id}/preview").status_code == 401
