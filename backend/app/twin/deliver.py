"""Delivery: how a Twin message comes to exist (INDEX "Delivery"; invariants 4 and 5).

Nothing about a candidate reaches an employer except as a `twin_messages` row, and an `answered` or `relayed` row is
accepted by the database only for an executed `twin_answer` action. `post` is the only place that executes one: it marks
the scheduled action executed and inserts the message in the same transaction, on a worker connection. The graph inserts
the other messages (`declined`, and the `unknown` holding message) through `say` without an action. No outbox and no
undo window: the candidate sees the message at once (AI_SYSTEM 7.6).

Every function here is safe to run twice: a second final message for a question is refused by the unique index
`twin_messages_one_final`, and `say` and `post` treat that refusal as "already done".
"""

from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.guardian import api as guardian
from app.platform import log, queue

from .logic import CLOSED_TEXT

FINAL = ("answered", "relayed", "declined")
CLOSING_TOPIC = "other"  # the topic of a decline that closes a session or settles a question that was never classified


def thread_id(question_id: UUID | str) -> str:
    return f"twin:{question_id}"


def enqueue_delete(conn: Connection, question_id: UUID | str, user_id: UUID | None) -> None:
    """Ask for the finished thread's checkpoints to be removed (the Phase 7 `graph.delete` job, which takes the thread's
    lock first, so a run still writing is waited for)."""
    queue.enqueue(
        conn,
        "graph.delete",
        {"thread_id": thread_id(question_id)},
        unique_key=f"graph.delete:{thread_id(question_id)}",
        user_id=user_id,
    )


def has_final(conn: Connection, question_id: UUID) -> bool:
    """True once the question has an answered, relayed or declined Twin message."""
    return (
        conn.execute(
            text(
                """SELECT 1 FROM twin_messages
                    WHERE in_reply_to = :q AND author = 'twin' AND answer_kind::text = ANY(CAST(:final AS text[]))"""
            ),
            {"q": question_id, "final": list(FINAL)},
        ).first()
        is not None
    )


def say(
    conn: Connection,
    question_id: UUID,
    *,
    kind: str,
    body: str,
    topic: str,
    session_id: UUID,
    user_id: UUID,
    organization_id: UUID,
) -> bool:
    """Insert a Twin message that needs no action (`declined`, or `unknown` for a holding message) in reply to the
    question. False when it was already there: a final message of the question exists, or this holding message with this
    body does."""
    if kind not in ("declined", "unknown"):
        raise ValueError("answered and relayed messages come only from post()")
    if has_final(conn, question_id):
        return False
    if kind == "unknown" and (
        conn.execute(
            text(
                """SELECT 1 FROM twin_messages
                    WHERE in_reply_to = :q AND author = 'twin' AND answer_kind = 'unknown' AND body = :b"""
            ),
            {"q": question_id, "b": body},
        ).first()
        is not None
    ):
        return False
    try:
        with conn.begin_nested():
            conn.execute(
                text(
                    """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic,
                                                  answer_kind, in_reply_to)
                       VALUES (:s, :u, :o, 'twin', :b, CAST(:t AS twin_topic), CAST(:k AS twin_answer_kind), :q)"""
                ),
                {
                    "s": session_id,
                    "u": user_id,
                    "o": organization_id,
                    "b": body,
                    "t": topic,
                    "k": kind,
                    "q": question_id,
                },
            )
    except IntegrityError:  # twin_messages_one_final: another run settled the question first
        return False
    return True


def post(conn: Connection, action_id: int) -> bool:
    """Release a scheduled `twin_answer`: mark it executed and insert the message it carries (`answered` with its cited
    facts, or `relayed` with the candidate's reply) in reply to the question, in this one transaction. False when there
    was nothing to do: the action is not scheduled, or it was already executed and the message exists (a retry after
    the commit). The database refuses the message unless the action is executed and both keys passed."""
    row = conn.execute(
        text(
            """SELECT a.id, a.user_id, a.status::text AS status, a.payload, a.inputs
                 FROM agent_actions a WHERE a.id = :id AND a.action_type = 'twin_answer' FOR UPDATE"""
        ),
        {"id": action_id},
    ).one_or_none()
    if row is None or row.status not in ("scheduled", "executed"):
        return False
    payload = row.payload
    question_id = UUID(str(payload["question_id"]))
    if row.status == "executed":
        return False
    if has_final(conn, question_id):  # a settled question takes no second answer: leave the action for the sweep
        return False
    kind = "answered" if payload["kind"] == "answer" else "relayed"
    try:
        with conn.begin_nested():
            guardian.mark_executed(conn, action_id)
            conn.execute(
                text(
                    """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body, topic,
                                                  answer_kind, cited_fact_ids, in_reply_to, agent_action_id)
                       VALUES (:s, :u, :o, 'twin', :b, CAST(:t AS twin_topic), CAST(:k AS twin_answer_kind),
                               CAST(:cited AS uuid[]), :q, :a)"""
                ),
                {
                    "s": UUID(str(payload["session_id"])),
                    "u": row.user_id,
                    "o": UUID(str(payload["organization_id"])),
                    "b": payload["body"],
                    "t": payload["topic"],
                    "k": kind,
                    "cited": [str(x) for x in payload.get("cited_fact_ids") or []],
                    "q": question_id,
                    "a": action_id,
                },
            )
    except DBAPIError:
        # The database refused the release (consent withdrawn, topic closed meanwhile, a closed session): the action
        # was not executed, so it is cancelled and the caller settles the question with `declined`.
        conn.execute(
            text(
                """UPDATE agent_actions SET status = 'cancelled', outcome_code = 'delivery_refused'
                    WHERE id = :id AND status = 'scheduled'"""
            ),
            {"id": action_id},
        )
        log.event("twin.delivery_refused", action_id=action_id)
        return False
    log.event("twin.posted", kind=kind, action_id=action_id)
    return True


def close_session(conn: Connection, session_id: UUID) -> int:
    """End a conversation. Open `twin_answer` actions of the session are cancelled, every question of it without a final
    message gets `declined` (CLOSED_TEXT) and its thread is queued for deletion, and only then is the session closed,
    since the database refuses every message in a closed session. Returns how many questions were settled."""
    session = conn.execute(
        text("SELECT user_id, organization_id, status::text AS status FROM twin_sessions WHERE id = :s FOR UPDATE"),
        {"s": session_id},
    ).one_or_none()
    if session is None or session.status != "open":
        return 0
    guardian.cancel_open(conn, None, "session_closed", session_id=session_id)
    settled = 0
    waiting = conn.execute(
        text(
            """SELECT m.id FROM twin_messages m
                WHERE m.session_id = :s AND m.author = 'employer'
                  AND NOT EXISTS (SELECT 1 FROM twin_messages t WHERE t.in_reply_to = m.id AND t.author = 'twin'
                                     AND t.answer_kind::text = ANY(CAST(:final AS text[])))
                ORDER BY m.created_at, m.id"""
        ),
        {"s": session_id, "final": list(FINAL)},
    ).all()
    for found in waiting:
        said = say(
            conn,
            found.id,
            kind="declined",
            body=CLOSED_TEXT,
            topic=CLOSING_TOPIC,
            session_id=session_id,
            user_id=session.user_id,
            organization_id=session.organization_id,
        )
        settled += int(said)
        enqueue_delete(conn, found.id, session.user_id)
    conn.execute(
        text("UPDATE twin_sessions SET status = 'closed', closed_at = now() WHERE id = :s AND status = 'open'"),
        {"s": session_id},
    )
    log.event("twin.session_closed", settled=settled)
    return settled
