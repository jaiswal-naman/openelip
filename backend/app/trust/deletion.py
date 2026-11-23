"""Account deletion (TRU-02; ARCHITECTURE 4.8). Two steps.

request_deletion runs in the API. A fresh emailed code proves the person is at their own mailbox, so a stolen session
alone cannot delete an account. In one transaction it opens the request, closes the account (status deleting: nobody can
sign in, every selection skips it), revokes the sessions, cancels the person's pending email and queues the erase job.

erase runs in the worker as the only role allowed to call erase_user. The ledger line is written before the erasure
commits, so a restore of an older backup can always be erased again; if the ledger cannot be written, nothing is
erased and the job is retried.

No campaigns, chat or hiring rows exist yet. The phases that add them add their own cancel step to request_deletion (a
campaign closed as cancelled, an open introduction withdrawn, agent tokens revoked) and register their tables as
deleters with trust.privacy.register, which the ownership and canary tests insist on.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import auth, config, log, relay
from app.platform.errors import ApiError
from app.platform.queue import NonRetryable, enqueue

DUE = timedelta(days=1)  # when erasure should be finished; the worker normally does it within a minute
# Where the erasure ledger is kept: the compose volume `erasure`, which the image makes writable by the app user.
LEDGER_DEFAULT = "/srv/erasure/ledger.jsonl"


@dataclass(frozen=True)
class Deletion:
    request_id: UUID
    revoked_sessions: int


def _open_deletion(conn: Connection, user_id: UUID) -> UUID | None:
    found: UUID | None = conn.execute(
        text(
            """SELECT id FROM data_requests
               WHERE subject_user_id = :u AND kind = 'deletion' AND status IN ('open', 'in_progress')"""
        ),
        {"u": user_id},
    ).scalar()
    return found


def request_deletion(conn: Connection, user_id: UUID, code: str) -> Deletion | int:
    """Close the person's account and queue its erasure, or say how many tries are left when the code was wrong.

    A wrong try is counted (auth._spend_code, the Phase 1 sign-in check, bound to the account's own address), so it is
    returned and not raised: the caller raises code_invalid once this transaction has committed, or the count would be
    rolled back and a stolen session could guess for ever. A person whose account is already closing gets the request
    that is closing it, so a repeat changes nothing.
    """
    account = conn.execute(
        text("SELECT email, status FROM users WHERE id = :u FOR NO KEY UPDATE"), {"u": user_id}
    ).first()
    if account is None:
        raise ApiError(401, "unauthenticated", "Sign in to continue.")
    if account.status == "deleting":
        again = _open_deletion(conn, user_id)
        if again is not None:
            return Deletion(again, 0)
    tries_left = auth._spend_code(conn, auth.normalize_email(account.email), code)
    if tries_left is not None:
        return tries_left
    request_id: UUID = conn.execute(
        text(
            """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at)
               VALUES (:u, :u, 'deletion', now() + :due) RETURNING id"""
        ),
        {"u": user_id, "due": DUE},
    ).scalar_one()
    conn.execute(text("UPDATE users SET status = 'deleting' WHERE id = :u"), {"u": user_id})
    revoked = conn.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE user_id = :u AND revoked_at IS NULL"), {"u": user_id}
    ).rowcount
    conn.execute(
        text(
            """UPDATE outbox SET status = 'cancelled', last_error_code = 'account_deleting'
               WHERE user_id = :u AND status = 'pending'"""
        ),
        {"u": user_id},
    )
    conn.execute(  # an export still waiting would only make one more copy of what is about to go
        text(
            """UPDATE data_requests SET status = 'failed'
               WHERE subject_user_id = :u AND kind = 'export' AND status IN ('open', 'in_progress')"""
        ),
        {"u": user_id},
    )
    enqueue(conn, "trust.erase", {"request_id": str(request_id)}, unique_key=f"trust.erase:{request_id}")
    return Deletion(request_id, revoked)


def ledger_path() -> Path:
    """Read each time, so a test or an operator can point it elsewhere. config.py is Phase 1's and does not know it."""
    return Path(os.environ.get("ERASURE_LEDGER_PATH") or LEDGER_DEFAULT)


def append_ledger(user_id: UUID, deleted_at: datetime) -> None:
    """One JSON line, flushed to disk: the bare id and the time, which is all the ledger may hold (ARCHITECTURE 8.2).

    A retry after a failed commit adds the same line again. That is harmless: replaying a line erases an absent person.
    """
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"user_id": str(user_id), "deleted_at": deleted_at.isoformat()}) + "\n"
    with path.open("a", encoding="utf-8") as ledger:
        ledger.write(line)
        ledger.flush()
        os.fsync(ledger.fileno())


def send_confirmation(address: str, request_id: UUID) -> str:
    """Tell the person it is done, once, straight to the mail server. Returns the relay's verdict (sent, retry, fail or
    unknown). Nothing is stored for a retry, because the address must not be: a missing message is accepted, as in
    the relay. The wording is a placeholder for the owner to approve."""
    settings = config.get()
    subject = f"Your {settings.product_name} account was deleted"
    body = (
        "You asked us to delete your account, and it is done. Your record, the files you uploaded and your sign-in "
        "details are removed from this service.\n\n"
        "We kept an internal reference number and the time of the deletion, so that it can be repeated if a backup "
        "is ever restored. We did not keep this email address.\n"
    )
    domain = settings.mail_from.rpartition("@")[2]
    message = relay._message(settings, f"<erased-{request_id}@{domain}>", relay.Outgoing(address, subject, body))
    return relay._send(settings, message)


def erase(conn: Connection, request_id: UUID) -> None:
    """The trust.erase job: erase the person the deletion request names, then tell them. Safe to run twice.

    The person comes from the verified request, never from the job's payload. The address is read first and held only
    in this function's memory, because erase_user removes the one place it was kept; once the erasure has committed, it
    is used for one message and dropped. The person's finished exports are cleared first: an archive is a copy of
    everything about them, and erase_user does not know it is there.
    """
    request = conn.execute(
        text(
            """SELECT r.subject_user_id AS person, r.status, u.email
               FROM data_requests r LEFT JOIN users u ON u.id = r.subject_user_id
               WHERE r.id = :r AND r.kind = 'deletion' FOR UPDATE OF r"""
        ),
        {"r": request_id},
    ).first()
    if request is None:
        raise NonRetryable("not a deletion request")
    if request.status == "completed":
        return
    address: str | None = request.email
    deliverable = address is not None and not relay.is_undeliverable(conn, address)
    conn.execute(
        text(
            "UPDATE data_requests SET export_archive = NULL WHERE subject_user_id = :u AND export_archive IS NOT NULL"
        ),
        {"u": request.person},
    )
    conn.execute(text("SELECT erase_user(:u, :r)"), {"u": request.person, "r": request_id})
    append_ledger(request.person, datetime.now(timezone.utc))  # before the commit: no ledger line, no erasure
    conn.commit()  # final; the message below is never sent for an erasure that rolled back
    log.event("trust.erased", data_request_id=request_id, outcome="completed")
    verdict = "not_sent"
    if deliverable and address:
        try:
            verdict = send_confirmation(address, request_id)
        except Exception:  # the erasure is done: a message that cannot be built must not fail the job
            verdict = "error"
    log.event("trust.confirmation", data_request_id=request_id, outcome=verdict)
