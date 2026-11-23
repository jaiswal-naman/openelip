"""HIR-04 (plan 06-05, task 3): an ask is one item of the morning brief, in the app and in the email.

Compose and the email resolver run on the rolled-back `conn`; the API test commits and reads GET /v1/briefs/{id} as the
real app_api role, where row-level security hides roles and organizations from the candidate. Everything is invented.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief import mail, views
from app.brief.api import compose
from app.hiring import api as hiring_api
from app.platform import relay
from app.trust import api as trust
from tests.brief.world import count
from tests.hiring import factories as hiring
from tests.match.factories import LOCAL_DATE, Verdict, make_job, make_run, make_shown_decision

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)
BASE = "http://localhost:8080"


def reader(conn, *, matches=0, name="Demo Ada"):
    """A real (not synthetic: the relay would refuse one) candidate with the brief_email consent and `matches` matches
    shown for LOCAL_DATE, as tests/brief/world.py makes them, who is also open to being asked about roles."""
    candidate = hiring.make_candidate(conn, name=name, email="reader@mail.test", is_synthetic=False)
    trust.grant_consent(conn, candidate.id, "brief_email", "test-wording")
    if matches:
        run = make_run(conn, candidate.id)
        for rank in range(1, matches + 1):
            job = make_job(
                conn,
                title=f"Role {rank}",
                description="Python.",
                requirements=[("Python.", "required")],
                external_id=f"job-{rank}",
            )
            make_shown_decision(
                conn, run, candidate.id, job, [Verdict("no_evidence")], rank_position=rank, summary=f"Summary {rank}."
            )
    return candidate


def ask_of(conn, candidate, *, org="Halden Demo", title="Data engineer", asked_at=None):
    role = hiring.make_open_role(conn, hiring.make_org(conn, org), title=title)
    return hiring.make_ask(conn, role, candidate, asked_at=asked_at)


def items(conn, brief_id, kind="introduction"):
    return conn.execute(
        text("SELECT position, introduction_id FROM brief_items WHERE brief_id = :b AND kind = :k ORDER BY position"),
        {"b": brief_id, "k": kind},
    ).all()


# --- hiring.api: the two reads ---


def test_asks_for_brief_lists_open_unexpired_asks_newest_first(conn):
    ada = reader(conn)
    now = datetime.now(timezone.utc)
    older = ask_of(conn, ada, org="Older Demo", title="Analyst", asked_at=now - timedelta(days=2))
    newer = ask_of(conn, ada, org="Newer Demo", title="Engineer", asked_at=now - timedelta(hours=1))
    ask_of(conn, ada, org="Expired Demo", asked_at=now - timedelta(days=8))
    accepted = ask_of(conn, ada, org="Accepted Demo")
    conn.execute(text("UPDATE introductions SET status = 'declined' WHERE id = :i"), {"i": accepted.id})

    asks = hiring_api.asks_for_brief(conn, ada.id)

    assert [(a.introduction_id, a.organization_name, a.role_title) for a in asks] == [
        (newer.id, "Newer Demo", "Engineer"),
        (older.id, "Older Demo", "Analyst"),
    ]
    assert all(a.expires_at > now for a in asks)


def test_brief_asks_names_the_callers_own_introductions_of_any_status_and_nothing_for_anyone_elses(conn):
    ada, bob = reader(conn), hiring.make_candidate(conn, name="Demo Bob")
    open_ask = ask_of(conn, ada, org="Open Demo", title="Role one")
    old = datetime.now(timezone.utc) - timedelta(days=8)
    gone = ask_of(conn, ada, org="Gone Demo", title="Role two", asked_at=old)
    conn.execute(text("UPDATE introductions SET status = 'expired' WHERE id = :i"), {"i": gone.id})
    bobs = ask_of(conn, bob, org="Bob Demo", title="Bob's role")

    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(ada.id)})
    named = hiring_api.brief_asks(conn, [open_ask.id, gone.id, bobs.id, uuid4()])

    assert {i: (a.organization_name, a.role_title) for i, a in named.items()} == {
        open_ask.id: ("Open Demo", "Role one"),
        gone.id: ("Gone Demo", "Role two"),
    }
    assert named[gone.id].expires_at == gone.expires_at
    assert hiring_api.brief_asks(conn, []) == {}
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))
    assert hiring_api.brief_asks(conn, [open_ask.id]) == {}  # nobody is the caller: nothing is named


# --- compose ---


def test_an_ask_is_an_item_ahead_of_the_matches_and_a_brief_of_only_asks_is_composed_and_emailed(conn, apply_links):
    ada = reader(conn)
    ask = ask_of(conn, ada)

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    assert result.status == "composed" and result.outbox_id is not None
    assert [(p, i) for p, i in items(conn, result.in_app_brief_id)] == [(1, ask.id)]


def test_introductions_come_before_match_items_and_the_matches_keep_their_order(conn, apply_links):
    ada = reader(conn, matches=2)
    first, second = ask_of(conn, ada, org="One Demo"), ask_of(conn, ada, org="Two Demo")

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    rows = conn.execute(
        text("SELECT position, kind::text AS kind FROM brief_items WHERE brief_id = :b ORDER BY position"),
        {"b": result.in_app_brief_id},
    ).all()
    assert [(r.position, r.kind) for r in rows] == [
        (1, "introduction"),
        (2, "introduction"),
        (3, "match"),
        (4, "match"),
    ]
    assert {i for _, i in items(conn, result.in_app_brief_id)} == {first.id, second.id}
    shown = views.brief_view(conn, result.in_app_brief_id, ada.id)["decisions"]
    assert [d["kind"] for d in shown] == ["introduction", "introduction", "match", "match"]


def test_the_next_days_brief_does_not_repeat_an_ask_and_composing_again_changes_nothing(conn, apply_links):
    ada = reader(conn, matches=1)
    ask = ask_of(conn, ada)
    first = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    rows = (count(conn, "briefs", user_id=ada.id), count(conn, "brief_items", user_id=ada.id))

    again = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    assert again == first
    assert (count(conn, "briefs", user_id=ada.id), count(conn, "brief_items", user_id=ada.id)) == rows
    tomorrow = compose(conn, ada.id, LOCAL_DATE + timedelta(days=1), now=MORNING + timedelta(days=1))
    assert items(conn, tomorrow.in_app_brief_id) == []
    assert count(conn, "brief_items", introduction_id=ask.id, kind="introduction") == 1


def test_an_ask_made_after_todays_brief_waits_for_the_first_brief_composed_after_it(conn, apply_links):
    ada = reader(conn, matches=1)
    compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    ask = ask_of(conn, ada)

    tomorrow = compose(conn, ada.id, LOCAL_DATE + timedelta(days=1), now=MORNING + timedelta(days=1))

    assert [i for _, i in items(conn, tomorrow.in_app_brief_id)] == [ask.id]


def test_an_expired_ask_is_not_put_in_a_brief(conn, apply_links):
    ada = reader(conn, matches=1)
    ask_of(conn, ada, asked_at=datetime.now(timezone.utc) - timedelta(days=8))

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    assert items(conn, result.in_app_brief_id) == []


# --- the view and the email ---


def test_the_view_still_names_an_ask_the_candidate_has_since_accepted(conn, apply_links):
    ada = reader(conn)
    ask = ask_of(conn, ada, org="Halden Demo", title="Data engineer")
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    trust.grant_introduction_consent(conn, ada.id, ask.org, ask.role, trust.INTRODUCTION_TEXT_VERSION)
    conn.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": ask.id})

    (item,) = views.brief_view(conn, result.in_app_brief_id, ada.id)["decisions"]

    assert item == {
        "kind": "introduction",
        "introduction_id": ask.id,
        "organization_name": "Halden Demo",
        "role_title": "Data engineer",
        "expires_at": ask.expires_at,
    }


def test_the_email_has_one_line_per_ask_with_link_and_expiry_and_nothing_the_candidate_would_share(conn, apply_links):
    ada = reader(conn, name="Demo Ada")
    one = ask_of(conn, ada, org="One Demo", title="Data engineer")
    two = ask_of(conn, ada, org="Two Demo", title="Analyst")
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # the relay's connection names nobody

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert isinstance(outgoing, relay.Outgoing) and outgoing.to == "reader@mail.test"
    body = outgoing.body
    assert "Introductions (2)" in body
    for ask, line in ((one, "One Demo about Data engineer"), (two, "Two Demo about Analyst")):
        assert f"- {line}. Answer by {ask.expires_at.date().isoformat()}." in body
        assert f"{BASE}/app/today?decision={ask.id}" in body
    assert body.count("/app/today?decision=") == 2
    for private in ("Demo Ada", "Python and SQL pipelines", "Senior data engineer", "reader@mail.test"):
        assert private not in body
    assert outgoing.subject == "Your morning brief"
    assert conn.execute(text("SELECT current_setting('app.user_id', true)")).scalar() in ("", None)  # put back


def test_a_brief_with_matches_and_asks_has_both_sections(conn, apply_links):
    ada = reader(conn, matches=1)
    ask_of(conn, ada)
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert outgoing.subject == "Your morning brief: 1 match"
    assert "Introductions (1)" in outgoing.body and "1. Role 1 at Initech Demo" in outgoing.body


# --- through the API as the candidate ---


def test_get_brief_returns_the_ask_with_names_as_the_candidate_under_row_level_security(
    engine, api_client, act_as, apply_links
):
    with engine.begin() as c:
        ada = reader(c, matches=1)
        ask = ask_of(c, ada, org="Halden Demo", title="Data engineer")
        accepted = ask_of(c, ada, org="Second Demo", title="Platform engineer")
        result = compose(c, ada.id, LOCAL_DATE, now=MORNING)
        trust.grant_introduction_consent(c, ada.id, accepted.org, accepted.role, trust.INTRODUCTION_TEXT_VERSION)
        c.execute(text("UPDATE introductions SET status = 'accepted' WHERE id = :i"), {"i": accepted.id})
    act_as(api_client, ada.id)

    response = api_client.get(f"/v1/briefs/{result.in_app_brief_id}")

    assert response.status_code == 200
    decisions = response.json()["data"]["decisions"]
    asks = [d for d in decisions if d["kind"] == "introduction"]
    assert [d["kind"] for d in decisions] == ["introduction", "introduction", "match"]
    assert {(d["introduction_id"], d["organization_name"], d["role_title"]) for d in asks} == {
        (str(ask.id), "Halden Demo", "Data engineer"),
        (str(accepted.id), "Second Demo", "Platform engineer"),
    }
    assert all(set(d) == {"kind", "introduction_id", "organization_name", "role_title", "expires_at"} for d in asks)


def test_another_person_cannot_read_the_ask_through_a_brief(engine, api_client, act_as, apply_links):
    with engine.begin() as c:
        ada = reader(c)
        ask_of(c, ada)
        result = compose(c, ada.id, LOCAL_DATE, now=MORNING)
        bob = hiring.make_candidate(c, name="Demo Bob")
    act_as(api_client, bob.id)

    assert api_client.get(f"/v1/briefs/{result.in_app_brief_id}").status_code == 404
