"""What a brief shows: the one shape that GET /v1/briefs/{id} returns and the email is written from (API.md 3.5),
and the list of past briefs (the in-app inbox).

A brief stores which matches it lists and in what order, nothing else. Everything the person reads is looked up when it
is asked for: the match lines come from app.match, the proposals waiting from the Career Record, what was done from the
match runs, and delivery from the outbox row of the email (the relay cannot update a brief row, so a brief's own status
never says whether its email went).
"""

import base64
import json
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, text

from app.hiring import api as hiring
from app.ingest import api as ingest
from app.match import api as match
from app.platform.errors import ApiError
from app.profile import api as profile

DONE_SUFFIX = {"nightly": "", "onboarding": " for your first matches", "on_demand": " on request"}
# What the outbox status of the email reads as: waiting, sent, or anything else (failed, cancelled, unknown, expired).
EMAIL_STATE = {"pending": "queued", "sending": "queued", "sent": "sent"}
NOT_ASSESSED = "Not yet assessed: the search ranked it high, but no model has checked it against your record."
UNCHECKED = "Apply link not re-checked this morning"

# One in-app brief per row, with what its delivery needs. The email brief of the same person and date is found by its
# date; its outbox row by the dedupe key the compose wrote.
_BRIEFS = """SELECT b.id, b.local_date, b.status::text AS status, b.covers_since, b.created_at,
                    (SELECT count(*) FROM brief_items i WHERE i.brief_id = b.id) AS item_count,
                    e.id IS NOT NULL AS has_email, o.status::text AS outbox_status, o.sent_at
             FROM briefs b
             LEFT JOIN briefs e ON e.user_id = b.user_id AND e.local_date = b.local_date AND e.channel = 'email'
             LEFT JOIN outbox o ON o.dedupe_key = 'brief:' || e.id::text
             WHERE b.user_id = :u AND b.channel = 'in_app'"""


def brief_view(conn: Connection, brief_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    """One in-app brief of this person as the API shows it, or None when there is no such brief of theirs."""
    brief = conn.execute(text(_BRIEFS + " AND b.id = :id"), {"u": user_id, "id": brief_id}).one_or_none()
    if brief is None:
        return None
    decisions = [*_introduction_items(conn, brief, user_id), *_match_decisions(conn, brief, user_id)]
    waiting = int(profile.get_record(conn, user_id)["open_proposals"])
    if waiting:
        decisions.append(_proposals_item(waiting))
    return {
        "id": brief.id,
        "local_date": brief.local_date,
        "status": brief.status,
        "delivery": _delivery(brief),
        "decisions": decisions,
        "done_since_last_brief": _done(conn, brief, user_id),
        "open_record_proposals": waiting,
    }


def list_briefs(
    conn: Connection, user_id: UUID, *, limit: int, cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """The person's in-app briefs, newest first, one page; the second value is the cursor of the next page or None."""
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    after = ""
    if cursor is not None:
        params["c_date"], params["c_id"] = _decode_cursor(cursor)
        after = " AND (b.local_date, b.id) < (:c_date, :c_id)"
    rows = conn.execute(text(_BRIEFS + after + " ORDER BY b.local_date DESC, b.id DESC LIMIT :n"), params).all()
    page = rows[:limit]
    listed = [
        {
            "id": r.id,
            "local_date": r.local_date,
            "status": r.status,
            "item_count": r.item_count,
            "delivery": _delivery(r),
        }
        for r in page
    ]
    return listed, (_encode_cursor(page[-1]) if len(rows) > limit else None)


def _introduction_items(conn: Connection, brief: Row[Any], user_id: UUID) -> list[dict[str, Any]]:
    """The brief's asks in the order it stored them, named as they are now (an ask since expired or accepted is still
    listed: the brief is a record of the morning). The names come from hiring.api.brief_asks, which answers for the
    caller only, so app.user_id is set to the brief's owner for that read: it already is on the candidate's own
    request, and it is not on the worker connection that writes the email. The previous value is put back."""
    ids = (
        conn.execute(
            text(
                "SELECT introduction_id FROM brief_items "
                "WHERE brief_id = :b AND kind = 'introduction' ORDER BY position"
            ),
            {"b": brief.id},
        )
        .scalars()
        .all()
    )
    if not ids:
        return []
    previous = conn.execute(text("SELECT current_setting('app.user_id', true)")).scalar() or ""
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(user_id)})
    try:
        named = hiring.brief_asks(conn, ids)
    finally:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": previous})
    return [
        {
            "kind": "introduction",
            "introduction_id": ask.introduction_id,
            "organization_name": ask.organization_name,
            "role_title": ask.role_title,
            "expires_at": ask.expires_at,
        }
        for ask in (named.get(i) for i in ids)
        if ask is not None
    ]


def _match_decisions(conn: Connection, brief: Row[Any], user_id: UUID) -> list[dict[str, Any]]:
    """The brief's matches in the order it stored them. A decision match.brief_matches no longer lists is skipped: its
    job has closed since, or the person's hard constraints now exclude it (the email is written from this when sent)."""
    stored = conn.execute(
        text("SELECT match_decision_id FROM brief_items WHERE brief_id = :b ORDER BY position"), {"b": brief.id}
    ).scalars()
    current = {m.decision_id: m for m in match.brief_matches(conn, user_id, brief.local_date)}
    kept = [current[decision_id] for decision_id in stored if decision_id in current]
    unchecked = ingest.unchecked_apply_links(conn, [m.job_id for m in kept if m.is_assessed])
    return [_match_item(m, m.job_id in unchecked) for m in kept]


def _match_item(m: match.BriefMatch, is_unchecked: bool) -> dict[str, Any]:
    decision = m.decision_id
    if m.is_assessed:
        feedback = f"/v1/matches/{decision}/feedback"
        actions = [
            {"action": "open", "method": "GET", "path": f"/v1/matches/{decision}"},
            {"action": "save", "method": "POST", "path": feedback, "body": {"kind": "save"}},
            {"action": "dismiss", "method": "POST", "path": feedback, "body": {"kind": "dismiss"}},
        ]
    else:  # no match page exists for a job nobody assessed; its ledger row is the place to look
        actions = [{"action": "open", "method": "GET", "path": f"/v1/decisions/{decision}"}]
    return {
        "kind": "match",
        "title": f"{m.title} at {m.company_name}",
        "why": (m.summary or "") if m.is_assessed else NOT_ASSESSED,
        "ref": {"decision_id": decision},
        "actions": actions,
        "is_assessed": m.is_assessed,
        "change_note": m.change_note,
        "note": UNCHECKED if is_unchecked else None,
    }


def _proposals_item(count: int) -> dict[str, Any]:
    """Changes to the record that someone suggested and the person has not answered. Each is confirmed or declined at
    POST /v1/me/events/{id}/confirm or /decline, from the list this item points to."""
    title = f"{count} changes to your record are" if count != 1 else "1 change to your record is"
    return {
        "kind": "record_proposals",
        "title": f"{title} waiting for your answer",
        "why": "Nothing changes in your record until you confirm it.",
        "ref": {"count": count},
        "actions": [{"action": "review", "method": "GET", "path": "/v1/me/events?status=proposed"}],
        "is_assessed": True,
        "change_note": None,
        "note": None,
    }


def _done(conn: Connection, brief: Row[Any], user_id: UUID) -> list[dict[str, Any]]:
    """One line for each match run that finished after the previous brief and before this one was made, so the same
    brief reads the same however late it is opened (BRF-02)."""
    if brief.covers_since is None:
        return []
    return [
        {
            "at": run.finished_at,
            "text": (
                f"Checked the index{DONE_SUFFIX.get(run.trigger, '')}: {run.shown} shown, {run.withheld} held back, "
                f"{run.filtered} filtered by your constraints"
            ),
            "ref": {"match_run_id": run.run_id},
        }
        for run in match.activity_since(conn, user_id, brief.covers_since)
        if run.finished_at <= brief.created_at
    ]


def _delivery(row: Row[Any]) -> dict[str, Any]:
    email = EMAIL_STATE.get(row.outbox_status or "", "failed") if row.has_email else None
    return {"in_app": row.status, "email": email, "email_sent_at": row.sent_at if email == "sent" else None}


def _encode_cursor(row: Row[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps([row.local_date.isoformat(), str(row.id)]).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[date, UUID]:
    try:
        local_date, brief_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return date.fromisoformat(local_date), UUID(brief_id)
    except (ValueError, TypeError):  # not base64, not JSON, not two values, not a date and an id
        raise ApiError(
            400, "invalid_request", "That cursor was not issued by this list. Start the list again."
        ) from None
