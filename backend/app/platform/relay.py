"""The relay that sends outbox rows by SMTP (ARCHITECTURE 5.5).

relay_once is called by the worker loop. A row is claimed with FOR UPDATE SKIP LOCKED and set to `sending` in a
statement that commits at once, so two relays never take the same row and no transaction stays open during the SMTP
call.

SMTP has no idempotency key. When the outcome of a send is not known (the connection broke after the message was handed
over, or the process died) the row is not retried: it waits in `sending` until its lease runs out, becomes `unknown`,
and the operator settles it with `python -m app.cli outbox resolve`. A rare missing email is accepted; a duplicate
is not.
"""

import smtplib
import ssl
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any
from urllib.parse import SplitResult, unquote, urlsplit
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import Connection, text
from sqlalchemy.engine import RowMapping

from . import config, db, log, mail, sessions

LEASE = timedelta(minutes=5)  # assumption: well past the SMTP timeout
SEND_TIMEOUT = 30  # seconds, for each step of the conversation with the mail server
RETRY_DELAYS = (30, 120, 600, 1800, 7200)  # seconds (ARCHITECTURE 5.7); one more failure ends the row
MAX_PER_CALL = 50  # so one call returns to the worker loop promptly


@dataclass(frozen=True)
class _Outgoing:
    to: str
    subject: str
    body: str


@dataclass(frozen=True)
class _Refusal:
    """A row that must not be sent: the status it ends in, and a content-free reason."""

    status: str
    code: str


def relay_once(conn: Connection) -> int:
    """Send the rows that are due and return how many were sent."""
    _expire_and_reconcile(conn)
    conn.commit()
    sent = 0
    for _ in range(MAX_PER_CALL):
        row = _claim(conn)
        conn.commit()  # `sending` is now visible to every other relay
        if row is None:
            break
        sent += _deliver(conn, row)
        conn.commit()
    return sent


def resolve(outbox_id: UUID, sent: bool) -> None:
    """The operator's answer for a row left in `unknown`: it was delivered, or it was not (`outbox resolve`)."""
    with db.transaction(role="worker") as conn:
        done = _settle(
            conn, outbox_id, "sent" if sent else "failed", None if sent else "operator_not_sent", was=("unknown",)
        )
    if not done:
        raise SystemExit(f"outbox {outbox_id}: there is no such row in state unknown")
    log.event("outbox.resolved", outbox_id=outbox_id, outcome="sent" if sent else "failed")


def _expire_and_reconcile(conn: Connection) -> None:
    expired = conn.execute(
        text(
            """UPDATE outbox SET status = 'cancelled', last_error_code = 'expired', payload = payload - 'code_sealed'
               WHERE status = 'pending' AND expires_at < now()"""
        )
    ).rowcount
    unknown = conn.execute(
        text(
            """UPDATE outbox SET status = 'unknown', last_error_code = 'lease_expired',
                                 payload = payload - 'code_sealed'
               WHERE status = 'sending' AND locked_at < now() - :lease"""
        ),
        {"lease": LEASE},
    ).rowcount
    if expired:
        log.event("outbox.expired", count=expired)
    if unknown:
        log.event("outbox.unknown", count=unknown)


def _claim(conn: Connection) -> RowMapping | None:
    return (
        conn.execute(
            text(
                """UPDATE outbox SET status = 'sending', locked_at = now(), attempts = attempts + 1
                   WHERE id = (SELECT id FROM outbox WHERE status = 'pending' AND not_before <= now()
                               ORDER BY not_before, created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
                   RETURNING id, template, payload, attempts"""
            )
        )
        .mappings()
        .one_or_none()
    )


def _deliver(conn: Connection, row: RowMapping) -> int:
    """Work out who gets what, send it, and record the outcome. Returns 1 when the message was sent."""
    outbox_id, kind = row["id"], row["template"]
    resolver = RESOLVERS.get(kind)
    resolved = resolver(conn, row["payload"]) if resolver else _Refusal("failed", "unknown_template")
    conn.commit()  # no transaction stays open while talking to the mail server
    if isinstance(resolved, _Refusal):
        _settle(conn, outbox_id, resolved.status, resolved.code)
        log.event("outbox.refused", outbox_id=outbox_id, kind=kind, outcome=resolved.code)
        return 0
    settings = config.get()
    message_id = f"<{outbox_id}@{settings.mail_from.rpartition('@')[2]}>"  # the operator finds the row by this id
    verdict = _send(settings, _message(settings, message_id, resolved))
    attempts = row["attempts"]
    if verdict == "sent":
        _settle(conn, outbox_id, "sent", message_id=message_id)
    elif verdict == "retry" and attempts <= len(RETRY_DELAYS):
        _retry_later(conn, outbox_id, attempts)
    elif verdict in ("retry", "fail"):
        _settle(conn, outbox_id, "failed", "smtp_permanent" if verdict == "fail" else "smtp_gave_up")
    # "unknown": the row stays in `sending`; when its lease runs out it becomes `unknown` and the operator decides
    log.event("outbox.send", outbox_id=outbox_id, kind=kind, outcome=verdict)
    return 1 if verdict == "sent" else 0


def _resolve_signin(conn: Connection, payload: dict[str, Any]) -> _Outgoing | _Refusal:
    try:
        login_code_id = UUID(str(payload["login_code_id"]))
        code = sessions.open_sealed(payload["code_sealed"])
    except (KeyError, TypeError, ValueError, InvalidToken):
        return _Refusal("failed", "bad_payload")
    login = conn.execute(
        text(
            """SELECT email, consumed_at IS NOT NULL AS is_used, expires_at <= now() AS is_expired,
                      CAST(ceil(extract(epoch FROM expires_at - now()) / 60) AS integer) AS minutes_left
               FROM login_codes WHERE id = :id"""
        ),
        {"id": login_code_id},
    ).one_or_none()
    if login is None or login.is_used or login.is_expired:
        return _Refusal("cancelled", "stale")  # replaced by a newer code, already used, or past its life
    if _is_undeliverable(conn, login.email):
        return _Refusal("cancelled", "undeliverable")
    subject, body = mail.signin(config.get().product_name, code, max(1, login.minutes_left))
    return _Outgoing(login.email, subject, body)


# One resolver per outbox template. Later phases add theirs here (a brief, a follow-up).
RESOLVERS: dict[str, Callable[[Connection, dict[str, Any]], _Outgoing | _Refusal]] = {"signin": _resolve_signin}


def _is_undeliverable(conn: Connection, address: str) -> bool:
    """ARCHITECTURE 5.5: never to an example.test address (reserved, it cannot exist) or to a synthetic account."""
    domain = address.rpartition("@")[2].lower()
    if domain == "example.test" or domain.endswith(".example.test"):
        return True
    return bool(
        conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM users WHERE lower(email) = :email AND is_synthetic)"),
            {"email": address.lower()},
        ).scalar_one()
    )


def _message(settings: config.Settings, message_id: str, outgoing: _Outgoing) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((settings.product_name, settings.mail_from))
    message["To"] = outgoing.to
    message["Subject"] = outgoing.subject
    message["Date"] = formatdate(usegmt=True)
    message["Message-ID"] = message_id
    message.set_content(outgoing.body)
    return message


def _send(settings: config.Settings, message: EmailMessage) -> str:
    """Hand the message to the mail server. Returns "sent"; "retry" (nothing was delivered, try later); "fail" (the
    server refused it for good); or "unknown" (the connection broke after the message was handed over, so it may have
    been delivered, and sending it again could duplicate it)."""
    url = urlsplit(settings.smtp_url)
    handed_over = False
    try:
        if url.scheme == "smtps":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                url.hostname or "localhost", url.port or 465, timeout=SEND_TIMEOUT, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(url.hostname or "localhost", url.port or 25, timeout=SEND_TIMEOUT)
        try:
            _log_in(server, url)
            handed_over = True
            server.send_message(message)
        finally:
            with suppress(smtplib.SMTPException, OSError):
                server.quit()
            server.close()
        return "sent"
    except Exception as error:  # the worker must never die here; every failure is classified below
        code = _refusal_code(error)
        if code is not None:
            return "fail" if code >= 500 else "retry"
        return "unknown" if handed_over else "retry"


def _log_in(server: smtplib.SMTP, url: SplitResult) -> None:
    """Use STARTTLS if the server offers it (certificate verified), then log in if the URL carries a login."""
    if not isinstance(server, smtplib.SMTP_SSL):
        server.ehlo()
        if server.has_extn("starttls"):
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
    if url.username:
        server.login(unquote(url.username), unquote(url.password or ""))


def _refusal_code(error: Exception) -> int | None:
    """The reply code when the server answered with a refusal; None for anything else (a broken connection)."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return min((code for code, _ in error.recipients.values()), default=None)
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code
    return None


def _retry_later(conn: Connection, outbox_id: UUID, attempts: int) -> None:
    conn.execute(
        text(
            """UPDATE outbox SET status = 'pending', locked_at = NULL, last_error_code = 'smtp_temporary',
                                 not_before = now() + :delay
               WHERE id = :id AND status = 'sending'"""
        ),
        {"id": outbox_id, "delay": timedelta(seconds=RETRY_DELAYS[attempts - 1])},
    )


def _settle(
    conn: Connection,
    outbox_id: UUID,
    status: str,
    code: str | None = None,
    *,
    message_id: str | None = None,
    was: tuple[str, ...] = ("sending", "unknown"),
) -> bool:
    """End a row: set its status and blank the sealed sign-in code. False if the row was not in a status in `was`."""
    done = conn.execute(
        text(
            """UPDATE outbox
               SET status = CAST(:status AS outbox_status), last_error_code = :code, locked_at = NULL,
                   sent_at = CASE WHEN :is_sent THEN now() END,
                   provider_message_id = COALESCE(:message_id, provider_message_id),
                   payload = payload - 'code_sealed'
               WHERE id = :id AND CAST(status AS text) = ANY(:was)"""
        ),
        {
            "id": outbox_id,
            "status": status,
            "code": code,
            "is_sent": status == "sent",
            "message_id": message_id,
            "was": list(was),
        },
    )
    return done.rowcount == 1
