"""CMP-04, GRD-04, GRD-07 (plan 07-06, task 3): what waits for the candidate's approval and what their agents did are items
of the morning brief, in the app and in the email, with links into the app and nothing to decide from email.

Compose and the email resolver run on the rolled-back `conn`; one API test commits and reads GET /v1/briefs/{id} as the
real app_api role. Campaigns, actions and approvals come from the 07-01 factories. Everything is invented.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief import mail, views
from app.brief.api import compose
from app.guardian import api as guardian
from app.platform import relay
from app.trust import api as trust
from tests.brief.test_introduction_items import ask_of, items
from tests.brief.world import count
from tests.campaigns.factories import make_action, make_approval, make_campaign, make_run
from tests.hiring import factories as hiring
from tests.match.factories import LOCAL_DATE, make_job

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)
BASE = "http://localhost:8080"
FOLLOW_UP = {
    "channel": "email",
    "to": "talent@lumen.example",
    "reply_to": "ada@example.test",
    "subject": "Following up on my application",
    "body": "Hello, I applied last week.",
}
LINKS = mail.Links(BASE, "tok")


def reader(conn):
    candidate = hiring.make_candidate(conn, name="Demo Ada", email="reader@mail.test", is_synthetic=False)
    trust.grant_consent(conn, candidate.id, "brief_email", "test-wording")
    return candidate


def campaign_of(conn, candidate, company="Lumen Grid"):
    job = make_job(
        conn,
        title="Site Reliability Engineer",
        description="Operate Python services.",
        requirements=[("Operate Python services", "required")],
        company_name=company,
        external_id=f"{company}-{candidate.id}",
    )
    return make_campaign(conn, candidate.id, job.id, stage="applied", follow_up_email="talent@lumen.example")


def follow_up(conn, candidate, campaign, *, status="awaiting_approval", approval="pending", expires="7 days", **kwargs):
    action = make_action(
        conn,
        make_run(conn, candidate.id, campaign, "chaser"),
        action_type="send_follow_up",
        payload=FOLLOW_UP,
        status=status,
        **kwargs,
    )
    approval_id = make_approval(conn, action, status=approval, expires_in=expires) if approval else None
    return action, approval_id


def previous_brief_a_day_ago(conn, candidate):
    """A quiet brief made yesterday, so that "since the previous brief" starts a day back."""
    compose(conn, candidate.id, LOCAL_DATE - timedelta(days=1), now=MORNING - timedelta(days=1))
    conn.execute(
        text("UPDATE briefs SET created_at = created_at - interval '1 day' WHERE user_id = :u"), {"u": candidate.id}
    )


def rows(conn, brief_id):
    return conn.execute(
        text(
            """SELECT position, kind::text AS kind, approval_id, agent_action_id FROM brief_items
                WHERE brief_id = :b ORDER BY position"""
        ),
        {"b": brief_id},
    ).all()


# ---- compose --------------------------------------------------------------------------------------------------------


def test_one_approval_item_per_pending_approval_soonest_expiry_first_at_most_five(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    made = [follow_up(conn, ada, campaign, expires=f"{n} days")[1] for n in (6, 2, 7, 1, 5, 3, 4)]

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    listed = [r.approval_id for r in rows(conn, result.in_app_brief_id)]
    soonest_first = [made[3], made[1], made[5], made[6], made[4]]  # 1, 2, 3, 4 and 5 days; 6 and 7 are left out
    assert listed == soonest_first and result.status == "composed" and result.outbox_id is not None


def test_approval_and_activity_items_come_after_asks_and_matches_with_ids_set(conn, apply_links):
    ada = reader(conn)
    previous_brief_a_day_ago(conn, ada)
    ask = ask_of(conn, ada)
    campaign = campaign_of(conn, ada)
    _, approval = follow_up(conn, ada, campaign)
    blocked, _ = follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="employer_blocked")

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    found = rows(conn, result.in_app_brief_id)
    assert [(r.position, r.kind) for r in found] == [(1, "introduction"), (2, "approval"), (3, "activity")]
    assert found[1].approval_id == approval and found[2].agent_action_id == blocked
    assert [i for _, i in items(conn, result.in_app_brief_id)] == [ask.id]


def test_activity_is_what_was_executed_blocked_or_failed_since_the_previous_brief_five_at_most(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="limit_reached")  # before: not listed
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))  # as the factories move a row's status
    conn.execute(text("UPDATE agent_actions SET created_at = created_at - interval '3 days'"))
    conn.execute(text("SELECT set_config('app.maintenance', '', true)"))
    previous_brief_a_day_ago(conn, ada)
    made = [
        follow_up(conn, ada, campaign, status="executed", approval=None, undo_until=datetime.now(timezone.utc))[0],
        follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="consent_missing")[0],
        follow_up(conn, ada, campaign, status="failed", approval=None, outcome_code="undeliverable")[0],
        follow_up(conn, ada, campaign, status="cancelled", approval=None, outcome_code="undone")[0],  # not an activity
        follow_up(
            conn,
            ada,
            campaign,
            status="scheduled",
            approval=None,
            undo_until=datetime.now(timezone.utc) + timedelta(minutes=5),
        )[0],
    ]
    for _ in range(4):
        made.append(follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="limit_reached")[0])

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    listed = [r.agent_action_id for r in rows(conn, result.in_app_brief_id)]
    assert len(listed) == 5 and set(listed) <= {made[0], made[1], made[2], *made[5:]}
    assert made[3] not in listed and made[4] not in listed


def test_composing_twice_for_one_day_still_yields_one_brief_and_one_set_of_items(conn, apply_links):
    ada = reader(conn)
    follow_up(conn, ada, campaign_of(conn, ada))

    first = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    shown = (count(conn, "briefs", user_id=ada.id), count(conn, "brief_items", user_id=ada.id))
    again = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    assert again == first
    assert (count(conn, "briefs", user_id=ada.id), count(conn, "brief_items", user_id=ada.id)) == shown


def test_a_person_with_no_campaign_items_gets_the_brief_they_got_before(conn, apply_links):
    ada = reader(conn)

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    view = views.brief_view(conn, result.in_app_brief_id, ada.id)

    assert result.status == "suppressed" and rows(conn, result.in_app_brief_id) == []
    assert view["decisions"] == [] and view["undoable"] == [] and result.outbox_id is None


# ---- the view -------------------------------------------------------------------------------------------------------


def test_the_view_reads_each_item_with_its_templated_sentence_link_and_undoable_follow_ups(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    _, approval = follow_up(conn, ada, campaign)
    previous_brief_a_day_ago(conn, ada)
    sent, _ = follow_up(conn, ada, campaign, status="executed", approval=None, undo_until=datetime.now(timezone.utc))
    blocked, _ = follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="employer_blocked")
    soon = datetime.now(timezone.utc) + timedelta(minutes=9)
    pending_send, _ = follow_up(conn, ada, campaign, status="scheduled", approval=None, undo_until=soon)
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    view = views.brief_view(conn, result.in_app_brief_id, ada.id)

    link = f"/app/campaigns/{campaign}"
    by_text = {d["text"]: d for d in view["decisions"]}
    waiting = by_text["Lumen Grid: a follow-up is waiting for your approval."]
    assert (waiting["kind"], waiting["campaign_id"], waiting["link"], waiting["approval_id"]) == (
        "approval",
        campaign,
        link,
        approval,
    )
    assert waiting["status"] == "pending"
    reason = guardian.explain("employer_blocked")
    block = by_text[f"Lumen Grid: a follow-up was blocked: {reason}"]
    assert (block["kind"], block["agent_action_id"], block["link"]) == ("activity", blocked, link)
    assert by_text["Lumen Grid: a follow-up was sent."]["agent_action_id"] == sent
    assert [(u["action_id"], u["campaign_id"], u["company_name"]) for u in view["undoable"]] == [
        (pending_send, campaign, "Lumen Grid")
    ]
    assert abs((view["undoable"][0]["undo_until"] - soon).total_seconds()) < 1


def test_an_approval_decided_before_the_brief_is_read_shows_its_current_state_not_pending(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    action, approval = follow_up(conn, ada, campaign)
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    conn.execute(
        text("UPDATE approvals SET status = 'rejected', decided_at = now(), decided_via = 'app' WHERE id = :i"),
        {"i": approval},
    )

    (item,) = views.brief_view(conn, result.in_app_brief_id, ada.id)["decisions"]

    assert item["status"] == "rejected" and item["text"] == "Lumen Grid: you declined a follow-up."
    assert item["approval_id"] == approval and action


def test_a_packet_approval_reads_ready_for_review(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    action = make_action(
        conn, make_run(conn, ada.id, campaign, "operator"), action_type="submit_application", status="awaiting_approval"
    )
    make_approval(conn, action)
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    (item,) = views.brief_view(conn, result.in_app_brief_id, ada.id)["decisions"]

    assert item["text"] == "Lumen Grid: your application packet is ready for review."


def test_another_persons_campaign_never_appears_in_a_brief(conn, apply_links):
    ada = reader(conn)
    bob = hiring.make_candidate(conn, name="Demo Bob", email="bob@mail.test", is_synthetic=False)
    follow_up(conn, bob, campaign_of(conn, bob, "Other Co"))

    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)

    assert rows(conn, result.in_app_brief_id) == []
    assert views.brief_view(conn, result.in_app_brief_id, ada.id)["undoable"] == []


# ---- the email ------------------------------------------------------------------------------------------------------


def test_the_email_lists_the_same_lines_with_app_links_and_has_nothing_to_decide_with(conn, apply_links):
    ada = reader(conn)
    campaign = campaign_of(conn, ada)
    follow_up(conn, ada, campaign)
    previous_brief_a_day_ago(conn, ada)
    follow_up(conn, ada, campaign, status="blocked", approval=None, outcome_code="employer_blocked")
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))  # the relay's connection names nobody

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert isinstance(outgoing, relay.Outgoing)
    body = outgoing.body
    link = f"{BASE}/app/campaigns/{campaign}"
    assert "Waiting for your approval (1)" in body and "What your agents did (1)" in body
    assert "- Lumen Grid: a follow-up is waiting for your approval." in body
    assert f"- Lumen Grid: a follow-up was blocked: {guardian.explain('employer_blocked')}" in body
    assert body.count(link) == 2
    urls = [word for word in body.split() if word.startswith("http")]
    assert all(u.startswith(BASE) for u in urls)
    assert not [u for u in urls if any(w in u.lower() for w in ("approve", "reject", "decline", "undo", "decide"))]


def test_the_email_of_a_brief_with_no_campaign_items_is_unchanged(conn, apply_links):
    ada = reader(conn)
    ask_of(conn, ada)
    result = compose(conn, ada.id, LOCAL_DATE, now=MORNING)
    conn.execute(text("SELECT set_config('app.user_id', '', true)"))

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert "approval" not in outgoing.body.lower() and "agents did" not in outgoing.body


def test_render_puts_the_two_sections_before_the_done_lines_and_one_link_per_item():
    view = {
        "local_date": "2026-11-23",
        "decisions": [
            {"kind": "approval", "text": "Co: a follow-up is waiting for your approval.", "link": "/app/campaigns/x"},
            {"kind": "activity", "text": "Co: a follow-up was sent.", "link": "/app/campaigns/y"},
        ],
        "done_since_last_brief": [{"text": "Checked the index"}],
    }

    _, body = mail.render("OpenElip", view, LINKS)

    assert body.index("Waiting for your approval") < body.index("What your agents did") < body.index("Done since")
    assert f"{BASE}/app/campaigns/x" in body and f"{BASE}/app/campaigns/y" in body


# ---- over HTTP ------------------------------------------------------------------------------------------------------


def test_the_brief_route_returns_the_new_items_and_the_undoable_list(engine, api_client, act_as, apply_links):
    with engine.begin() as c:
        ada = reader(c)
        campaign = campaign_of(c, ada)
        _, approval = follow_up(c, ada, campaign)
        follow_up(
            c,
            ada,
            campaign,
            status="scheduled",
            approval=None,
            undo_until=datetime.now(timezone.utc) + timedelta(minutes=9),
        )
        result = compose(c, ada.id, LOCAL_DATE, now=MORNING)
    act_as(api_client, ada.id)

    response = api_client.get(f"/v1/briefs/{result.in_app_brief_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    (item,) = data["decisions"]
    assert (
        item["kind"] == "approval"
        and item["approval_id"] == str(approval)
        and item["link"] == f"/app/campaigns/{campaign}"
    )
    assert item["text"] == "Lumen Grid: a follow-up is waiting for your approval." and item["agent_action_id"] is None
    assert [u["campaign_id"] for u in data["undoable"]] == [str(campaign)]
