"""BRF-01 and BRF-03: the brief as an email (template brief_v1): the text, the relay's resolver, and one whole morning.

The first tests write the text from a hand-made view. The resolver tests use rows on the rolled-back `conn`. The last
two go through the real database roles, the real queue and relay, and a small real SMTP server (conftest.py).
"""

import email
from datetime import date, datetime
from email import policy
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.brief import jobs, mail, unsubscribe, views
from app.brief.api import compose
from app.platform import db, queue, relay
from app.trust import api as trust
from tests.brief.world import world
from tests.match.factories import LOCAL_DATE
from tests.match.fixtures.run.world import add_constraint
from tests.platform.test_relay import relay_now

IST = ZoneInfo("Asia/Kolkata")
MORNING = datetime(2026, 11, 23, 6, 10, tzinfo=IST)
BASE = "http://localhost:8080"  # APP_BASE_URL's development default
LINKS = mail.Links(BASE, "TOKEN123")


# ---- the text --------------------------------------------------------------------------------------------------


def item(title, why="A fit.", *, decision=None, assessed=True, change_note=None, note=None):
    return {
        "kind": "match",
        "title": title,
        "why": why,
        "ref": {"decision_id": decision or uuid4()},
        "actions": [],
        "is_assessed": assessed,
        "change_note": change_note,
        "note": note,
    }


def proposals(count=2):
    return {
        "kind": "record_proposals",
        "title": f"{count} changes to your record are waiting for your answer",
        "why": "Nothing changes in your record until you confirm it.",
        "ref": {"count": count},
        "actions": [],
        "is_assessed": True,
        "change_note": None,
        "note": None,
    }


def view(*decisions, done=()):
    return {
        "id": uuid4(),
        "local_date": date(2026, 11, 23),
        "status": "composed",
        "delivery": {"in_app": "composed", "email": "queued", "email_sent_at": None},
        "decisions": list(decisions),
        "done_since_last_brief": [{"at": MORNING, "text": line, "ref": {}} for line in done],
        "open_record_proposals": 0,
    }


def test_the_subject_counts_the_matches_and_says_nothing_of_them_when_there_are_none():
    three = view(item("A at X"), item("B at X"), item("C at X"))
    one = view(item("A at X"))
    none = view(item("Z at X", assessed=False), proposals())

    assert mail.render("Careers", three, LINKS)[0] == "Your morning brief: 3 matches"
    assert mail.render("Careers", one, LINKS)[0] == "Your morning brief: 1 match"
    assert mail.render("Careers", none, LINKS)[0] == "Your morning brief"


def test_each_match_is_listed_with_its_title_its_summary_and_its_link():
    first, second = uuid4(), uuid4()
    brief = view(
        item("Data engineer at Initech Demo", "Meets 4 of 5 requirements.", decision=first),
        item(
            "Analyst at Hooli Demo",
            "Meets 2 of 3 requirements.",
            decision=second,
            change_note="The pay is now stated.",
            note="Apply link not re-checked this morning",
        ),
    )

    _, body = mail.render("Careers", brief, LINKS)

    lines = body.splitlines()
    assert "1. Data engineer at Initech Demo" in lines and "   Meets 4 of 5 requirements." in lines
    assert f"   {BASE}/app/matches/{first}" in lines and f"   {BASE}/app/matches/{second}" in lines
    assert "   Changed since you saw it: The pay is now stated." in lines
    assert "   Apply link not re-checked this morning." in lines


def test_the_other_sections_the_settings_link_and_the_visible_unsubscribe_link_are_there():
    brief = view(
        item("Job nobody read at X", assessed=False),
        proposals(),
        done=["Checked the index: 3 shown, 37 held back, 120 filtered by your constraints"],
    )

    subject, body = mail.render("Careers", brief, LINKS)

    assert subject == "Your morning brief"
    assert "Not yet assessed (1)" in body and "- Job nobody read at X" in body and f"  {BASE}/app/today" in body
    assert f"2 changes to your record are waiting for your answer: {BASE}/app/record" in body
    assert "- Checked the index: 3 shown, 37 held back, 120 filtered by your constraints" in body
    assert f"Settings: {BASE}/app/settings?section=brief" in body
    assert f"Stop this email: {BASE}/unsubscribe?t=TOKEN123" in body
    assert body.endswith("Sent by Careers.\n")


def test_a_line_break_in_a_job_title_cannot_start_a_new_line_of_the_mail():
    hostile = "Data\r\nengineer at Initech\nBcc: someone@mail.test\r\n\r\nClick here"

    _, body = mail.render("Careers", view(item(hostile, why="Fit.\r\nBut")), LINKS)

    assert "\r" not in body
    assert "1. Data engineer at Initech Bcc: someone@mail.test Click here" in body.splitlines()
    assert "   Fit. But" in body.splitlines()


# ---- the resolver ----------------------------------------------------------------------------------------------


def composed(conn, **world_options):
    user, decisions = world(conn, email="reader@mail.test", **world_options)
    return user, decisions, compose(conn, user, LOCAL_DATE, now=MORNING)


def test_the_resolver_writes_the_message_to_the_person_with_the_two_unsubscribe_headers(conn, apply_links):
    user, decisions, result = composed(conn)

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert isinstance(outgoing, relay.Outgoing) and outgoing.to == "reader@mail.test"
    assert outgoing.subject == "Your morning brief: 3 matches"
    for number, decision in enumerate(decisions, start=1):
        assert f"{number}. Role {number} at Initech Demo" in outgoing.body
        assert f"{BASE}/app/matches/{decision}" in outgoing.body
    header, post = outgoing.headers["List-Unsubscribe"], outgoing.headers["List-Unsubscribe-Post"]
    assert post == "List-Unsubscribe=One-Click"
    assert header.startswith(f"<{BASE}/v1/briefs/unsubscribe?t=") and header.endswith(">")
    one_click = parse_qs(urlsplit(header[1:-1]).query)["t"][0]
    visible = outgoing.body.split(f"Stop this email: {BASE}/unsubscribe?t=")[1].split()[0]
    assert unsubscribe.verify(one_click, now=MORNING) == user == unsubscribe.verify(visible, now=MORNING)


def test_a_constraint_confirmed_after_the_brief_was_composed_keeps_that_job_out_of_the_email_and_the_view(
    conn, apply_links
):
    """The email is written when it is sent, an hour or so after the brief: a person who excluded a company in between
    must not get it (invariant 1)."""
    user, decisions = world(conn, email="reader@mail.test")
    conn.execute(text("UPDATE jobs SET company_name = 'Hooli Demo' WHERE external_id = 'job-2'"))
    result = compose(conn, user, LOCAL_DATE, now=MORNING)
    assert len(views.brief_view(conn, result.in_app_brief_id, user)["decisions"]) == 3

    add_constraint(conn, user, "excluded_company", company_names=["Hooli Demo"])  # between 05:30 and 06:40

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert isinstance(outgoing, relay.Outgoing) and outgoing.subject == "Your morning brief: 2 matches"
    assert "1. Role 1 at Initech Demo" in outgoing.body and "2. Role 3 at Initech Demo" in outgoing.body
    assert "Hooli Demo" not in outgoing.body and f"/app/matches/{decisions[1]}" not in outgoing.body
    kept = [d["ref"]["decision_id"] for d in views.brief_view(conn, result.in_app_brief_id, user)["decisions"]]
    assert kept == [decisions[0], decisions[2]]


def test_a_job_that_closed_after_the_brief_was_composed_is_not_in_the_email(conn, apply_links):
    user, decisions, result = composed(conn)
    conn.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE external_id = 'job-3'"))

    outgoing = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert outgoing.subject == "Your morning brief: 2 matches"
    assert "Role 3" not in outgoing.body and f"/app/matches/{decisions[2]}" not in outgoing.body


def test_a_consent_taken_back_after_the_brief_was_composed_cancels_the_email(conn, apply_links):
    user, _, result = composed(conn)
    trust.withdraw_consent(conn, user, "brief_email")

    refused = mail.resolve(conn, {"brief_id": str(result.in_app_brief_id)})

    assert refused == relay.Refusal("cancelled", "consent_withdrawn")


def test_a_brief_that_is_gone_and_a_payload_that_is_wrong_are_refused_in_their_own_words(conn):
    assert mail.resolve(conn, {"brief_id": str(uuid4())}) == relay.Refusal("cancelled", "stale")
    for payload in ({}, {"brief_id": "not-an-id"}, {"brief_id": None}):
        assert mail.resolve(conn, payload) == relay.Refusal("failed", "bad_payload")


def test_the_relays_undeliverable_rule_covers_the_brief_too(conn, apply_links):
    user, _, result = composed(conn)
    payload = {"brief_id": str(result.in_app_brief_id)}
    # Plan 08-01: the rule lives once in the relay core, on the resolved recipient; the resolver only names the person.
    conn.execute(text("UPDATE users SET email = 'someone@example.test' WHERE id = :u"), {"u": user})
    out = mail.resolve(conn, payload)
    assert isinstance(out, relay.Outgoing) and relay.is_undeliverable(conn, out.to)  # an address that cannot exist
    conn.execute(text("UPDATE users SET email = 'reader@mail.test', is_synthetic = true WHERE id = :u"), {"u": user})
    out = mail.resolve(conn, payload)
    assert isinstance(out, relay.Outgoing) and relay.is_undeliverable(conn, out.to)  # a demo or wind-tunnel account
    conn.execute(text("UPDATE users SET is_synthetic = false WHERE id = :u"), {"u": user})
    out = mail.resolve(conn, payload)
    assert isinstance(out, relay.Outgoing) and not relay.is_undeliverable(conn, out.to)  # deliverable otherwise


# ---- through the relay -----------------------------------------------------------------------------------------


class Frozen(datetime):
    """datetime whose now() is the morning under test, so the compose job's `now` does not depend on the day the test
    runs (the email's noon expiry would otherwise have passed)."""

    @classmethod
    def now(cls, tz=None):
        return MORNING.astimezone(tz)


def release_now(engine):
    """It is 06:40 and a bit more: the email is due, and its noon expiry is still ahead."""
    with engine.begin() as c:
        c.execute(
            text("UPDATE outbox SET not_before = now() - interval '1 minute', expires_at = now() + interval '1 hour'")
        )


def parse(raw: dict) -> email.message.EmailMessage:
    return email.message_from_bytes(raw["data"], policy=policy.default)


def test_a_whole_morning_the_sweep_the_compose_job_the_relay_and_the_one_click_unsubscribe(
    engine, api_client, smtp, apply_links, monkeypatch
):
    with engine.begin() as c:
        user, decisions = world(c, email="reader@mail.test")
        c.execute(text("UPDATE users SET record_confirmed_at = :t"), {"t": datetime(2026, 11, 20, 12, 0, tzinfo=IST)})
    monkeypatch.setattr(jobs, "datetime", Frozen)

    with db.transaction(role="worker") as conn:  # the sweep, at 05:31, as the worker role
        assert jobs.queue_composes(conn, datetime(2026, 11, 23, 5, 31, tzinfo=IST)) == 1
    assert queue.run_once(lambda: db.transaction(role="worker"), "test-worker") == 1  # the compose job
    release_now(engine)
    assert relay_now() == 1

    (sent,) = smtp.messages
    message = parse(sent)
    assert sent["to"] == "reader@mail.test" and message["Subject"] == "Your morning brief: 3 matches"
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    link = message["List-Unsubscribe"].strip("<>")
    assert link.startswith(f"{BASE}/v1/briefs/unsubscribe?t=")
    body = message.get_content()
    assert all(f"{BASE}/app/matches/{decision}" in body for decision in decisions)
    with engine.connect() as c:
        row = c.execute(text("SELECT status::text AS status, sent_at FROM outbox")).one()
        assert row.status == "sent" and row.sent_at is not None
        assert (
            c.execute(text("SELECT status::text FROM queue_jobs WHERE kind = 'brief.compose'")).scalar_one()
            == "succeeded"
        )

    token = parse_qs(urlsplit(link).query)["t"][0]  # what a mail client does: one POST, no cookie, no key
    answered = api_client.post(f"/v1/briefs/unsubscribe?t={token}", data={"List-Unsubscribe": "One-Click"})
    assert answered.status_code == 200 and answered.json()["data"] == {"is_brief_email_on": False}
    with db.transaction(user) as conn:
        states = {c.purpose: c.granted for c in trust.current_consents(conn, user)}
    assert states["brief_email"] is False


def test_an_email_whose_consent_was_taken_back_before_it_was_due_is_cancelled_not_sent(
    engine, api_client, smtp, apply_links
):
    with engine.begin() as c:
        user, _, _ = composed(c)
    release_now(engine)
    with engine.begin() as c:
        trust.withdraw_consent(c, user, "brief_email")

    assert relay_now() == 0 and smtp.messages == []
    with engine.connect() as c:
        row = c.execute(text("SELECT status::text AS status, last_error_code FROM outbox")).one()
    assert (row.status, row.last_error_code) == ("cancelled", "consent_withdrawn")
