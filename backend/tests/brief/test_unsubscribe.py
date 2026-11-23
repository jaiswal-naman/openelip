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

from app.brief import unsubscribe
from app.platform import config

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
