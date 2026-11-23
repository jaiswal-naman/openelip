"""The signed link in the brief email that turns the email off (BRF-03; threats T-04-24 and T-04-25).

The token names one person and says when it stops working, and nothing else: base64url(user id + expiry) "."
base64url of an HMAC-SHA256 over those bytes, keyed with APP_LINK_KEY and bound to this one purpose so a signature
made for another signed link can never pass here. It is checked in constant time and only in its one canonical
spelling, so a token with any character changed is refused, including a last character whose unused bits a decoder
would ignore.

The email's List-Unsubscribe header and its visible link both carry it. A mail client or the /unsubscribe page POSTs
it; a GET does nothing (a link scanner that fetches every URL in a message must not unsubscribe anyone).
"""

import base64
import hashlib
import hmac
from datetime import datetime, timedelta
from uuid import UUID

from app.platform import config

TOKEN_LIFE = timedelta(days=60)  # assumption: a brief still in a mailbox two months on can still be unsubscribed from
_PURPOSE = b"brief-unsubscribe:"
_USER_BYTES, _EXPIRY_BYTES = 16, 8


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(payload: bytes) -> bytes:
    return hmac.new(config.get().link_key.encode(), _PURPOSE + payload, hashlib.sha256).digest()


def sign(user_id: UUID, *, now: datetime) -> str:
    """A token for this person that works for TOKEN_LIFE from `now` (an aware datetime)."""
    expires = int((now + TOKEN_LIFE).timestamp())
    payload = user_id.bytes + expires.to_bytes(_EXPIRY_BYTES, "big")
    return f"{_b64(payload)}.{_b64(_mac(payload))}"


def verify(token: str, *, now: datetime) -> UUID | None:
    """The person a token names, or None for a token that is forged, altered, made with another key or expired."""
    payload_text, _, mac_text = token.partition(".")
    try:
        payload, mac = _unb64(payload_text), _unb64(mac_text)
    except ValueError:  # includes binascii.Error and non-ASCII text
        return None
    if len(payload) != _USER_BYTES + _EXPIRY_BYTES or (_b64(payload), _b64(mac)) != (payload_text, mac_text):
        return None  # wrong length, or not the one spelling sign() writes
    if not hmac.compare_digest(mac, _mac(payload)):
        return None
    if int.from_bytes(payload[_USER_BYTES:], "big") < int(now.timestamp()):
        return None
    return UUID(bytes=payload[:_USER_BYTES])
