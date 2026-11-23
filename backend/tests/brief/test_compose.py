"""BRF-01 and BRF-02: composing one person's brief for one local date.

Rows are invented (tests/match/factories.py) and written on the `conn` fixture's connection, which is rolled back. The
apply-link check is the real verify_live over a scripted fetch (conftest.py), so a job's row changes as it would.
"""

import logging
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief import compose as composing
from app.brief import views
from app.brief.api import compose
from app.platform import http
from app.profile import api as profile
from app.trust import api as trust
from tests.match.factories import (
    LOCAL_DATE,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)  # before any email would be released


def world(conn, count=3, *, consent=True):
    """A person with a confirmed record, the brief_email consent (unless left out), and `count` shown matches today."""
    user = make_user(conn)
    make_record(conn, user)
    if consent:
        trust.grant_consent(conn, user, "brief_email", "test-wording")
    run = make_run(conn, user)
    decisions = []
    for rank in range(1, count + 1):
        job = make_job(
            conn,
            title=f"Role {rank}",
            description="Python.",
            requirements=[("Python.", "required")],
            external_id=f"job-{rank}",
        )
        decisions.append(
            make_shown_decision(
                conn, run, user, job, [Verdict("no_evidence")], rank_position=rank, summary=f"Summary {rank}."
            )
        )
    return user, decisions


def count(conn, table, **where):
    clause = " AND ".join(f"{column} = :{column}" for column in where) or "true"
    return conn.execute(text(f"SELECT count(*) FROM {table} WHERE {clause}"), where).scalar_one()


def everything(conn, user):
    """The rows a compose writes for a person, counted: briefs, items and outbox rows."""
    return (
        count(conn, "briefs", user_id=user),
        count(conn, "brief_items", user_id=user),
        count(conn, "outbox", user_id=user),
    )


# ---- what is written -------------------------------------------------------------------------------------------


def test_three_matches_make_an_in_app_brief_with_items_in_rank_order_and_one_queued_email(conn, apply_links):
    user, decisions = world(conn)

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert result.status == "composed" and result.email_brief_id and result.outbox_id
    briefs = conn.execute(
        text("SELECT channel::text AS channel, status::text AS status FROM briefs WHERE user_id = :u ORDER BY channel"),
        {"u": user},
    ).all()
    assert [(b.channel, b.status) for b in briefs] == [("email", "queued"), ("in_app", "composed")]
    items = conn.execute(
        text("SELECT position, kind::text AS kind, match_decision_id FROM brief_items WHERE brief_id = :b ORDER BY 1"),
        {"b": result.in_app_brief_id},
    ).all()
    assert [(i.position, i.kind, i.match_decision_id) for i in items] == [
        (1, "match", decisions[0]),
        (2, "match", decisions[1]),
        (3, "match", decisions[2]),
    ]
    assert count(conn, "brief_items", brief_id=result.email_brief_id) == 0  # the email is built from the in-app brief
    row = conn.execute(text("SELECT * FROM outbox WHERE id = :id"), {"id": result.outbox_id}).mappings().one()
    assert (row["template"], row["status"], row["user_id"]) == ("brief_v1", "pending", user)
    assert row["dedupe_key"] == f"brief:{result.email_brief_id}"
    assert row["payload"] == {"brief_id": str(result.in_app_brief_id)}  # ids only: no title, no address, no summary


def test_composing_again_creates_nothing_and_returns_the_same_ids(conn, apply_links):
    user, _ = world(conn)
    first = compose(conn, user, LOCAL_DATE, now=MORNING)
    written = everything(conn, user)
    asked = len(apply_links.asked)

    again = compose(conn, user, LOCAL_DATE, now=MORNING + timedelta(minutes=3))

    assert again == first and everything(conn, user) == written == (2, 3, 1)
    assert len(apply_links.asked) == asked, "a repeat asks no apply link a second time"


def test_a_compose_that_loses_the_race_returns_what_the_other_one_made(conn, apply_links, monkeypatch):
    """Two composes at once: both look, neither sees a brief, both try. The database lets one in."""
    user, _ = world(conn)
    first = compose(conn, user, LOCAL_DATE, now=MORNING)
    written = everything(conn, user)
    real, looks = composing._existing, []

    def blind_the_first_time(*args):
        looks.append(args)
        return None if len(looks) == 1 else real(*args)

    monkeypatch.setattr(composing, "_existing", blind_the_first_time)

    second = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert len(looks) == 2 and second == first and everything(conn, user) == written


def test_the_email_goes_out_between_0630_and_0655_local_and_expires_at_noon(conn, apply_links):
    user, _ = world(conn)

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    row = conn.execute(text("SELECT not_before, expires_at FROM outbox WHERE id = :id"), {"id": result.outbox_id}).one()
    released = row.not_before.astimezone(IST)
    assert released.date() == LOCAL_DATE and time(6, 30) <= released.time() <= time(6, 55)
    assert row.expires_at.astimezone(IST) == datetime(2026, 11, 23, 12, 0, tzinfo=IST)
    planned = conn.execute(
        text("SELECT send_after FROM briefs WHERE id = :id"), {"id": result.email_brief_id}
    ).scalar_one()
    assert planned == row.not_before


def test_an_email_composed_after_its_slot_is_released_now_and_never_in_the_past(conn, apply_links):
    user, _ = world(conn)
    late = datetime(2026, 11, 23, 7, 10, tzinfo=IST)

    result = compose(conn, user, LOCAL_DATE, now=late)

    released = conn.execute(text("SELECT not_before FROM outbox WHERE id = :id"), {"id": result.outbox_id}).scalar_one()
    assert released == late


def test_the_release_time_is_the_same_every_time_for_one_person_and_spread_over_the_window():
    people = [uuid4() for _ in range(60)]

    windows = [composing.release_window(p, LOCAL_DATE, IST, MORNING)[0] for p in people]

    assert windows == [composing.release_window(p, LOCAL_DATE, IST, MORNING)[0] for p in people]
    assert all(time(6, 30) <= w.astimezone(IST).time() <= time(6, 55) for w in windows)
    assert len({w for w in windows}) >= 10, "everyone at 06:30 would be one burst for the mail server"


def test_a_person_who_has_not_agreed_to_the_email_gets_the_in_app_brief_only(conn, apply_links):
    user, _ = world(conn, consent=False)

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert result.status == "composed" and result.email_brief_id is None and result.outbox_id is None
    assert everything(conn, user) == (1, 3, 0)


def test_a_person_who_took_the_consent_back_gets_no_email(conn, apply_links):
    user, _ = world(conn)
    trust.withdraw_consent(conn, user, "brief_email")

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert result.outbox_id is None and count(conn, "briefs", user_id=user, channel="email") == 0


def test_a_day_with_nothing_to_say_is_stored_suppressed_and_sends_nothing(conn, apply_links):
    user = make_user(conn)
    make_record(conn, user)
    trust.grant_consent(conn, user, "brief_email", "test-wording")

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert result.status == "suppressed" and result.email_brief_id is None and result.outbox_id is None
    assert everything(conn, user) == (1, 0, 0)
    status = conn.execute(text("SELECT status::text FROM briefs WHERE id = :id"), {"id": result.in_app_brief_id})
    assert status.scalar_one() == "suppressed"


# ---- the apply-link check --------------------------------------------------------------------------------------


def test_a_dead_link_leaves_the_brief_an_unsure_one_stays_with_a_note(conn, apply_links):
    user, decisions = world(conn)
    apply_links.answers[apply_links.url("job-1")] = 404
    apply_links.answers[apply_links.url("job-2")] = 503  # the site had a bad minute: nothing is learned

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    view = views.brief_view(conn, result.in_app_brief_id, user)
    assert [d["title"] for d in view["decisions"]] == ["Role 2 at Initech Demo", "Role 3 at Initech Demo"]
    assert [d["note"] for d in view["decisions"]] == ["Apply link not re-checked this morning", None]
    positions = conn.execute(
        text("SELECT position, match_decision_id FROM brief_items WHERE brief_id = :b ORDER BY 1"),
        {"b": result.in_app_brief_id},
    ).all()
    assert [(p.position, p.match_decision_id) for p in positions] == [(1, decisions[1]), (2, decisions[2])]
    states = conn.execute(
        text("SELECT external_id, status::text AS status, apply_check_ok FROM jobs ORDER BY external_id")
    ).all()
    assert [(s.external_id, s.status, s.apply_check_ok) for s in states] == [
        ("job-1", "closed", False),
        ("job-2", "open", None),
        ("job-3", "open", True),
    ]


def test_a_failed_fetch_is_unsure_too_and_the_job_stays(conn, apply_links):
    user, _ = world(conn, count=1)
    apply_links.answers[apply_links.url("job-1")] = http.FetchFailed("timeout")

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    (only,) = views.brief_view(conn, result.in_app_brief_id, user)["decisions"]
    assert only["note"] == "Apply link not re-checked this morning"


def test_when_every_link_is_dead_there_is_nothing_to_say(conn, apply_links):
    user, _ = world(conn, count=2)
    apply_links.answers[apply_links.url("job-1")] = 410
    apply_links.answers[apply_links.url("job-2")] = 404

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert result.status == "suppressed" and result.outbox_id is None


# ---- what the brief lists --------------------------------------------------------------------------------------


def test_the_match_items_carry_open_save_and_dismiss_pointing_at_the_match_endpoints(conn, apply_links):
    user, decisions = world(conn, count=1)
    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    view = views.brief_view(conn, result.in_app_brief_id, user)

    (item,) = view["decisions"]
    decision = decisions[0]
    assert item["kind"] == "match" and item["is_assessed"] is True and item["why"] == "Summary 1."
    assert item["ref"] == {"decision_id": decision} and item["title"] == "Role 1 at Initech Demo"
    assert item["actions"] == [
        {"action": "open", "method": "GET", "path": f"/v1/matches/{decision}"},
        {"action": "save", "method": "POST", "path": f"/v1/matches/{decision}/feedback", "body": {"kind": "save"}},
        {
            "action": "dismiss",
            "method": "POST",
            "path": f"/v1/matches/{decision}/feedback",
            "body": {"kind": "dismiss"},
        },
    ]
    assert (view["id"], view["local_date"], view["status"]) == (result.in_app_brief_id, LOCAL_DATE, "composed")


def test_a_job_shown_again_carries_the_note_of_what_changed(conn, apply_links):
    user = make_user(conn)
    make_record(conn, user)
    run = make_run(conn, user)
    job = make_job(conn, title="Role 1", description="Python.", requirements=[("Python.", "required")], external_id="a")
    make_shown_decision(conn, run, user, job, [Verdict("no_evidence")], change_note="The pay is now stated.")

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    (item,) = views.brief_view(conn, result.in_app_brief_id, user)["decisions"]
    assert item["change_note"] == "The pay is now stated."


def test_with_no_scoring_that_day_the_items_are_the_jobs_nobody_assessed_and_say_so(conn, apply_links):
    user = make_user(conn)
    make_record(conn, user)
    run = make_run(conn, user, evidence_prompt_version=None)  # the run could not score: no key, or no consent
    held = []
    for rank in range(1, 8):  # seven retrieved, and a brief lists five at most
        job = make_job(
            conn, title=f"Job {rank}", description="Go.", requirements=[("Go.", "required")], external_id=f"n{rank}"
        )
        held.append(make_decision(conn, run, user, job, retrieval_rank=rank))

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    decisions = views.brief_view(conn, result.in_app_brief_id, user)["decisions"]
    assert [d["ref"]["decision_id"] for d in decisions] == held[:5]
    first = decisions[0]
    assert first["is_assessed"] is False and first["why"].startswith("Not yet assessed")
    assert first["actions"] == [{"action": "open", "method": "GET", "path": f"/v1/decisions/{held[0]}"}]
    assert apply_links.asked == [], "no apply link is vouched for on a job nobody assessed"


def test_a_proposal_waiting_makes_a_brief_and_an_item_that_points_at_the_list(conn, apply_links):
    user = make_user(conn)
    make_record(conn, user)
    profile.propose_change(
        conn, user, origin="chat", entity="fact", operation="create", after={"kind": "skill", "statement": "Go"}
    )

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    view = views.brief_view(conn, result.in_app_brief_id, user)
    assert result.status == "composed" and view["open_record_proposals"] == 1
    (item,) = view["decisions"]
    assert item["kind"] == "record_proposals" and item["title"].startswith("1 change to your record is waiting")
    assert item["actions"] == [{"action": "review", "method": "GET", "path": "/v1/me/events?status=proposed"}]


# ---- what was done since the last brief ------------------------------------------------------------------------


def _hours_ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _covers_since(conn, brief_id):
    return conn.execute(text("SELECT covers_since FROM briefs WHERE id = :b"), {"b": brief_id}).scalar_one()


def test_done_since_the_last_brief_lists_the_runs_between_the_last_brief_and_this_one(conn, apply_links):
    user = make_user(conn)
    make_record(conn, user)
    conn.execute(text("UPDATE users SET record_confirmed_at = :t WHERE id = :u"), {"t": _hours_ago(100), "u": user})
    covered = make_run(conn, user, local_date=LOCAL_DATE - timedelta(days=3), finished_at=_hours_ago(72))
    mine = make_run(conn, user, filtered_count=120, finished_at=_hours_ago(3))
    job = make_job(conn, title="Role 1", description="Go.", requirements=[("Go.", "required")], external_id="d")
    make_shown_decision(conn, mine, user, job, [Verdict("no_evidence")])
    for external in ("e", "f"):  # two jobs the run scored and held back
        held = make_job(
            conn, title=external, description="Go.", requirements=[("Go.", "required")], external_id=external
        )
        make_decision(conn, mine, user, held, reasons=("weak_fit",))
    # the previous brief was made 40 hours ago: a run that finished before that was covered by it
    previous = compose(conn, user, LOCAL_DATE - timedelta(days=1), now=MORNING)
    conn.execute(
        text("UPDATE briefs SET created_at = :t WHERE id = :b"), {"t": _hours_ago(40), "b": previous.in_app_brief_id}
    )
    not_yet = make_run(conn, user, trigger="on_demand", finished_at=_hours_ago(-2))  # finishes after this brief is made

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    done = views.brief_view(conn, result.in_app_brief_id, user)["done_since_last_brief"]
    assert [(d["ref"]["match_run_id"], d["text"]) for d in done] == [
        (mine, "Checked the index: 1 shown, 2 held back, 120 filtered by your constraints")
    ]
    assert covered != not_yet != mine
    assert abs((_covers_since(conn, result.in_app_brief_id) - _hours_ago(40)).total_seconds()) < 5


def test_the_first_brief_covers_everything_since_the_record_was_confirmed(conn, apply_links):
    user, _ = world(conn, count=1)
    confirmed = _hours_ago(50)
    conn.execute(text("UPDATE users SET record_confirmed_at = :t WHERE id = :u"), {"t": confirmed, "u": user})

    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert _covers_since(conn, result.in_app_brief_id) == confirmed


def test_composing_says_ids_and_counts_in_the_log_and_never_a_title(conn, apply_links, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    user, _ = world(conn)

    compose(conn, user, LOCAL_DATE, now=MORNING)

    assert "brief.composed" in caplog.text and "Role 1" not in caplog.text and "Summary" not in caplog.text


def test_only_the_owner_can_view_an_in_app_brief_and_the_email_brief_is_not_one(conn, apply_links):
    user, _ = world(conn)
    other = make_user(conn)
    result = compose(conn, user, LOCAL_DATE, now=MORNING)

    assert views.brief_view(conn, result.in_app_brief_id, other) is None
    assert views.brief_view(conn, result.email_brief_id, user) is None, "the email brief is not an inbox entry"
