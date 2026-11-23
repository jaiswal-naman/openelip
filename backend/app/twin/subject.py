"""What the Twin teaches the Guardian when it is imported (INDEX "Guardian for `twin_answer`"):

  * the `twin` subject reader: a Twin session as the Guardian's subject (closed session or an introduction that is no
    longer accepted = closed subject);
  * the approve hook for `twin_answer`: the candidate's reply to a question the record does not answer (checked,
    stored as a `candidate` message and optionally proposed as a record change), handed back as the approval's edit;
  * the event subscription: `guardian.twin_action_changed` runs the `twin.resume` job on the interactive queue.

The Guardian (layer 4) may not import the Twin (layer 6); everything here reaches it through its registries. The hook
runs on the candidate's own API connection, so it opens no worker or match transaction.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.guardian import api as guardian
from app.hiring import api as hiring
from app.platform import events
from app.platform.errors import ApiError
from app.profile import api as profile
from app.trust import api as trust

from . import api as twin
from .jobs import RESUME

EVENT = "guardian.twin_action_changed"
REPLY_MIN, REPLY_MAX = 1, 4000
RECORD_REASON = "Your reply to an employer's question."


def read_gate(conn: Connection, session_id: UUID) -> guardian.SubjectGate | None:
    """The Twin session as the Guardian reads it, or None when this connection cannot see it."""
    row = conn.execute(
        text(
            """SELECT s.id, s.user_id, s.organization_id, s.status::text AS status, i.status::text AS introduction,
                      u.status::text = 'active' AS active
                 FROM twin_sessions s
                 JOIN introductions i ON i.id = s.introduction_id
                 JOIN users u ON u.id = s.user_id
                WHERE s.id = :id"""
        ),
        {"id": session_id},
    ).one_or_none()
    if row is None:
        return None
    return guardian.SubjectGate(
        kind="twin",
        id=row.id,
        user_id=row.user_id,
        user_active=row.active,
        paused_reason=None,
        closed=row.status != "open" or row.introduction != "accepted",
        job_id=0,
        organization_id=row.organization_id,
        follow_up_email=None,
        budget_microusd=0,
    )


def _refused(field: str, issue: str, **details: Any) -> ApiError:
    return ApiError(
        422,
        "validation_failed",
        "One or more fields are invalid.",
        {"fields": [{"field": field, "issue": issue}], **details},
    )


def approve_hook(
    conn: Connection, user_id: UUID, view: dict[str, Any], *, reply: Any, save_to_record: bool
) -> guardian.HookResult:
    """The Twin's part of an approval. A drafted answer is approved as it stands: a reply is refused. A relay needs the
    candidate's reply (1 to 4,000 characters, no pay or contact details): it is stored as the candidate's message about
    the question, optionally proposed for the record (it waits for them), and returned as the approval's edit so the
    relay carries it. Nothing reaches the employer here: the worker still runs both keys and posts it."""
    payload = view["payload"] or {}
    if payload.get("kind") != "relay":
        if reply is not None or save_to_record:
            raise _refused("reply", "not_supported")
        return guardian.HookResult()
    if not isinstance(reply, str) or not REPLY_MIN <= len(reply.strip()) <= REPLY_MAX:
        raise _refused("reply", "required" if reply is None else "invalid_length")
    body = reply.strip()
    if trust.restricted_matches(body):
        raise _refused("reply", "restricted_data", reason="restricted_data")
    question_id = UUID(str(payload["question_id"]))
    try:
        with conn.begin_nested():
            conn.execute(
                text(
                    """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, in_reply_to)
                       VALUES (:s, :u, :o, 'candidate', :b, :q)"""
                ),
                {
                    "s": UUID(str(payload["session_id"])),
                    "u": user_id,
                    "o": UUID(str(payload["organization_id"])),
                    "b": body,
                    "q": question_id,
                },
            )
    except DBAPIError:  # a closed session, withdrawn consent or a question that is not theirs
        raise ApiError(409, "conflict", "This conversation no longer takes a reply.", {"state": "closed"}) from None
    proposal = None
    if save_to_record:
        event_id = profile.propose_change(
            conn,
            user_id,
            origin="agent",
            entity="fact",
            operation="create",
            after={"kind": "other", "statement": body},
            origin_ref=str(question_id),
            reason=RECORD_REASON,
        )
        proposal = {"event_id": str(event_id), "status": "proposed"}
    seen = twin.question_view(conn, question_id)
    labels = hiring.introduction_labels(conn, [UUID(str(payload["introduction_id"]))])
    label = labels.get(UUID(str(payload["introduction_id"])))
    return guardian.HookResult(
        edits={"body": body},
        extra={
            "question": {
                "question_id": str(question_id),
                "state": "waiting_for_candidate" if seen is None else seen.state,
            },
            "relayed_to": None if label is None else label.organization_name,
            "record_proposal": proposal,
        },
    )


guardian.register_subject("twin", read_gate)
guardian.register_approve_hook("twin_answer", approve_hook)
events.subscribe(EVENT, RESUME, queue=twin.QUEUE)
