"""The Twin's conversations as the pages and the API read them (API 2.14 and 3.14): sessions with their questions.

Everything runs on the caller's own connection, so row-level security decides what is there: the candidate reads every
session about them with every question, holding message, answer and their own reply; an employer member reads the
sessions of an introduction of their organization with the questions and the Twin's released messages, never a reply.
The question's own shape comes from `twin.api.question_view`; this module adds the session around it.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.hiring import api as hiring

from . import api as twin


def view_dict(view: twin.QuestionView, *, candidate: bool) -> dict[str, Any]:
    """API 3.14's question. The candidate's own reading adds the pending approval, whether a reply is wanted and their
    reply; an employer's reading never has those keys."""
    answer = view.answer
    data: dict[str, Any] = {
        "question_id": view.question_id,
        "session_id": view.session_id,
        "introduction_id": view.introduction_id,
        "topic": view.topic,
        "state": view.state,
        "question": {"body": view.question.body, "created_at": view.question.created_at},
        "answer": None
        if answer is None
        else {
            "author": answer.author,
            "answer_kind": answer.answer_kind,
            "body": answer.body,
            "citations": [{"fact_id": c.fact_id, "quote": c.quote} for c in answer.citations],
            "created_at": answer.created_at,
        },
    }
    if candidate:
        data["approval_id"] = view.approval_id
        data["needs_reply"] = view.needs_reply
        data["reply"] = None if view.reply is None else {"body": view.reply.body, "created_at": view.reply.created_at}
    return data


def _sessions(conn: Connection, where: str, params: dict[str, Any], *, candidate: bool) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            f"""SELECT s.id, s.introduction_id, s.status::text AS status FROM twin_sessions s
                 WHERE {where} ORDER BY s.created_at DESC, s.id"""
        ),
        params,
    ).all()
    labels = hiring.introduction_labels(conn, list({r.introduction_id for r in rows}))
    sessions = []
    for row in rows:
        ids = [
            r.id
            for r in conn.execute(
                text(
                    """SELECT id FROM twin_messages WHERE session_id = :s AND author = 'employer'
                       ORDER BY created_at, id"""
                ),
                {"s": row.id},
            )
        ]
        views = [twin.question_view(conn, question_id) for question_id in ids]
        label = labels.get(row.introduction_id)
        sessions.append(
            {
                "session_id": row.id,
                "introduction_id": row.introduction_id,
                "organization": None
                if label is None
                else {"id": label.organization_id, "name": label.organization_name},
                "role": None if label is None else {"id": label.role_id, "title": label.role_title},
                "status": row.status,
                "questions_used": len(ids),
                "questions_left": max(0, twin.SESSION_QUESTIONS - len(ids)),
                "questions": [view_dict(v, candidate=candidate) for v in views if v is not None],
            }
        )
    return sessions


def list_for_candidate(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """Every session about the candidate, newest first, each with all its questions oldest first."""
    return _sessions(conn, "s.user_id = :u", {"u": user_id}, candidate=True)


def list_for_employer(conn: Connection, introduction_id: UUID) -> list[dict[str, Any]]:
    """The sessions of one introduction as its organization may read them (no reply, no approval)."""
    return _sessions(conn, "s.introduction_id = :i", {"i": introduction_id}, candidate=False)


def is_member_of_an_organization(conn: Connection, user_id: UUID) -> bool:
    """True when the person is an active member of any organization (the employer side of the list route)."""
    return (
        conn.execute(
            text("SELECT 1 FROM org_members WHERE user_id = :u AND status = 'active' LIMIT 1"), {"u": user_id}
        ).first()
        is not None
    )
