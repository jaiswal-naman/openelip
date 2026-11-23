"""BRF-03: the signed one-click unsubscribe. The token is checked here; the route that takes it is checked below.

Everything is invented: people are random ids, addresses are mail.test.
"""

import hashlib
import hmac
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.brief import unsubscribe
from app.platform import config
from app.trust import api as trust
from tests.brief.conftest import new_person

NOW = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)


# ---- the token -------------------------------------------------------------------------------------------------


def test_a_token_names_the_person_it_was_made_for():
    person = uuid4()

    token = unsubscribe.sign(person, now=NOW)

    assert unsubscribe.verify(token, now=NOW) == person
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", token), "it goes into a URL as it is"


def test_a_token_with_any_one_character_changed_is_refused():
    """Every position, including the last one, whose unused bits a plain decoder would ignore."""
    person = uuid4()
    token = unsubscribe.sign(person, now=NOW)

    for position, original in enumerate(token):
        for other in "AB":
            if other == original or original == ".":
                continue
            altered = token[:position] + other + token[position + 1 :]
            assert unsubscribe.verify(altered, now=NOW) is None, f"position {position} accepted"
    assert unsubscribe.verify(token.replace(".", ".."), now=NOW) is None
    assert unsubscribe.verify(token + "A", now=NOW) is None
    assert unsubscribe.verify(token[:-1], now=NOW) is None


def test_a_token_made_with_another_key_is_refused(monkeypatch):
    person = uuid4()
    token = unsubscribe.sign(person, now=NOW)
    other_key = replace(config.get(), link_key="another-link-key-entirely")
    monkeypatch.setattr(config, "get", lambda: other_key)

    assert unsubscribe.verify(token, now=NOW) is None
    assert unsubscribe.verify(unsubscribe.sign(person, now=NOW), now=NOW) == person  # and it works under its own key


def test_a_signature_made_for_another_purpose_is_refused():
    """The purpose prefix: a signature over the same bytes made for another signed link unsubscribes no one."""
    person = uuid4()
    payload = person.bytes + int((NOW + unsubscribe.TOKEN_LIFE).timestamp()).to_bytes(8, "big")
    foreign = hmac.new(config.get().link_key.encode(), b"export-link:" + payload, hashlib.sha256).digest()
    token = f"{unsubscribe._b64(payload)}.{unsubscribe._b64(foreign)}"

    assert unsubscribe.verify(token, now=NOW) is None


def test_a_token_stops_working_after_sixty_days():
    person = uuid4()
    token = unsubscribe.sign(person, now=NOW)

    assert unsubscribe.TOKEN_LIFE == timedelta(days=60)
    assert unsubscribe.verify(token, now=NOW + timedelta(days=60) - timedelta(seconds=1)) == person
    assert unsubscribe.verify(token, now=NOW + timedelta(days=60) + timedelta(seconds=1)) is None


def test_a_change_of_the_expiry_is_caught_by_the_signature():
    """Moving the expiry later without the key is the one edit a person would want; the signature covers it."""
    person = uuid4()
    token = unsubscribe.sign(person, now=NOW)
    payload_text, _, mac_text = token.partition(".")
    payload = bytearray(unsubscribe._unb64(payload_text))
    payload[-1] ^= 0x01
    forged = f"{unsubscribe._b64(bytes(payload))}.{mac_text}"

    assert unsubscribe.verify(forged, now=NOW) is None


@pytest.mark.parametrize(
    "garbage",
    ["", ".", "..", "abc", "a.b", "é.é", "A" * 200, "=" * 10 + "." + "=" * 10, "\x00.\x00", " ", "null"],
)
def test_garbage_is_refused_without_an_error(garbage):
    assert unsubscribe.verify(garbage, now=NOW) is None


def test_the_token_names_nothing_but_the_person_and_the_expiry():
    person = uuid4()
    payload_text = unsubscribe.sign(person, now=NOW).partition(".")[0]

    assert len(unsubscribe._unb64(payload_text)) == 24
    assert UUID(bytes=unsubscribe._unb64(payload_text)[:16]) == person


# ---- the one-click POST ----------------------------------------------------------------------------------------

FORM = {"List-Unsubscribe": "One-Click"}


def subscribed(engine, email="ada@mail.test", *, agreed=True):
    """A committed person who has (or has not) agreed to the brief email."""
    person = new_person(engine, email)
    if agreed:
        with engine.begin() as c:
            trust.grant_consent(c, person, "brief_email", "test-wording")
    return person


def link(person, **options):
    now = options.get("now", datetime.now(timezone.utc))
    return f"/v1/briefs/unsubscribe?t={unsubscribe.sign(person, now=now)}"


def email_is_on(engine, person):
    with engine.connect() as c:
        return {s.purpose: s.granted for s in trust.current_consents(c, person)}["brief_email"]


def audit_rows(engine, person, event="brief.unsubscribed"):
    with engine.connect() as c:
        return c.execute(
            text(
                """SELECT actor_type::text AS actor, details FROM audit_log
                   WHERE subject_user_id = :u AND event_type = :e"""
            ),
            {"u": person, "e": event},
        ).all()


def test_one_post_with_the_token_alone_withdraws_the_consent_with_no_redirect_and_audits_it(engine, api_client):
    ada = subscribed(engine)

    answered = api_client.post(link(ada), data=FORM, follow_redirects=False)  # no cookie, no Idempotency-Key

    assert answered.status_code == 200 and "location" not in answered.headers
    body = answered.json()
    assert body["success"] is True and body["data"] == {"is_brief_email_on": False}
    assert email_is_on(engine, ada) is False
    (row,) = audit_rows(engine, ada)
    assert row.actor == "candidate" and row.details == {"channel": "email"}
    assert len(audit_rows(engine, ada, "consent.withdrawn")) == 1


def test_the_multipart_form_a_mail_client_may_send_works_the_same(engine, api_client):
    ada = subscribed(engine)

    answered = api_client.post(link(ada), files={"List-Unsubscribe": (None, "One-Click")}, follow_redirects=False)

    assert answered.status_code == 200 and email_is_on(engine, ada) is False


def test_a_second_post_answers_the_same_and_changes_nothing(engine, api_client):
    ada = subscribed(engine)
    url = link(ada)
    first = api_client.post(url, data=FORM)
    with engine.connect() as c:
        consent_rows = c.execute(text("SELECT count(*) FROM consents")).scalar_one()
        audit_count = c.execute(text("SELECT count(*) FROM audit_log")).scalar_one()

    second = api_client.post(url, data=FORM)

    assert (first.status_code, second.status_code) == (200, 200) and second.json()["data"] == first.json()["data"]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM consents")).scalar_one() == consent_rows
        assert c.execute(text("SELECT count(*) FROM audit_log")).scalar_one() == audit_count


def test_a_person_who_never_agreed_is_answered_the_same_and_nothing_is_written(engine, api_client):
    bob = subscribed(engine, "bob@mail.test", agreed=False)

    answered = api_client.post(link(bob), data=FORM)

    assert answered.status_code == 200 and audit_rows(engine, bob) == []


def test_the_token_is_the_person_a_session_of_someone_else_changes_nothing(engine, api_client, act_as):
    ada, bob = subscribed(engine), subscribed(engine, "bob@mail.test")
    act_as(api_client, bob)

    answered = api_client.post(link(ada), data=FORM)

    assert answered.status_code == 200
    assert email_is_on(engine, ada) is False and email_is_on(engine, bob) is True


def test_a_forged_altered_or_expired_link_is_a_404_and_changes_nothing(engine, api_client, monkeypatch):
    ada = subscribed(engine)
    good = unsubscribe.sign(ada, now=datetime.now(timezone.utc))
    last = "A" if good[-1] != "A" else "B"
    other_key = replace(config.get(), link_key="another-link-key-entirely")
    with monkeypatch.context() as m:
        m.setattr(config, "get", lambda: other_key)
        foreign = unsubscribe.sign(ada, now=datetime.now(timezone.utc))
    expired = unsubscribe.sign(ada, now=datetime.now(timezone.utc) - unsubscribe.TOKEN_LIFE - timedelta(minutes=1))

    for token in (good[:-1] + last, foreign, expired, "garbage", "a.b", " "):
        answered = api_client.post(f"/v1/briefs/unsubscribe?t={token}", data=FORM)
        assert answered.status_code == 404 and answered.json()["error"]["code"] == "not_found", token
    assert email_is_on(engine, ada) is True and audit_rows(engine, ada) == []


def test_a_post_without_the_one_click_body_is_refused_and_changes_nothing(engine, api_client):
    ada = subscribed(engine)
    url = link(ada)

    for sent in ({}, {"List-Unsubscribe": "Yes"}, {"unsubscribe": "One-Click"}):
        answered = api_client.post(url, data=sent)
        assert answered.status_code == 422 and answered.json()["error"]["code"] == "validation_failed", sent
    assert api_client.post("/v1/briefs/unsubscribe", data=FORM).status_code == 422  # no token at all
    assert email_is_on(engine, ada) is True


def test_a_get_on_the_link_does_nothing_so_a_scanner_that_fetches_it_unsubscribes_nobody(engine, api_client):
    ada = subscribed(engine)

    fetched = api_client.get(link(ada), follow_redirects=False)

    assert fetched.status_code in (401, 405, 422) and "location" not in fetched.headers
    assert email_is_on(engine, ada) is True and audit_rows(engine, ada) == []
