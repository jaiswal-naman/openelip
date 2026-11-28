"""What other modules may call from app.twin: the question intake and the question view (plan 09-01; TWN-01, TWN-04).

  ask(conn, ...)             an employer member, or an organization's agent, asks the candidate's Twin a question
  question_view(conn, id)    one question as the caller may read it: its state, topic, answer and citations
  wait(id, read, seconds)    poll a view until the Twin has posted something, or the time is up

Everything here runs on the caller's own connection, on the API login: row-level security decides who may ask and who
may read, and nothing opens a worker or match transaction. An employer question is stored without a topic (the Twin
classifies it later), the Twin runs in the worker through the `twin.resume` job, and a released answer is a Twin message
that exists only as an executed `twin_answer` action (the database enforces that). Plan 09-03 adds `get_scopes` and
`put_scopes` to this module.

The state of a question is derived, never stored: no Twin message about it yet is `pending`; the latest holding message
(`unknown`) is `waiting_for_candidate`; a final message (`answered`, `relayed`, `declined`) names itself.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from app.guardian import api as guardian
from app.hiring import api as hiring
from app.platform import queue

from .jobs import RESUME

__all__ = [
    "ANSWER_WAIT_SECONDS",
    "QUESTION_MAX",
    "QUESTION_MIN",
    "SESSION_QUESTIONS",
    "AnswerView",
    "Asked",
    "Citation",
    "ConsentMissing",
    "IntroductionNotFound",
    "InvalidQuestion",
    "LimitReached",
    "QuestionBody",
    "QuestionView",
    "ReplyView",
    "SessionClosed",
    "TwinError",
    "ask",
    "question_view",
    "wait",
]

SESSION_QUESTIONS = 40  # assumption, AI_SYSTEM 8: questions one session takes before it stops
ANSWER_WAIT_SECONDS = 30  # assumption: classify 10 s + answer 20 s
QUESTION_MIN = 5
QUESTION_MAX = 1000
POLL_SECONDS = 0.5
QUEUE = "interactive"
RESUME_ATTEMPTS = 20

State = Literal["pending", "waiting_for_candidate", "answered", "relayed", "declined"]


class TwinError(Exception):
    """Base of what the intake refuses with."""


class IntroductionNotFound(TwinError):
    """The introduction is not visible to the caller, is not accepted, or is another organization's."""


class SessionClosed(TwinError):
    """The introduction's Twin session is closed: it takes no more questions."""


class LimitReached(TwinError):
    """The session already holds SESSION_QUESTIONS questions."""


class ConsentMissing(TwinError):
    """The candidate withdrew the introduction consent: the Twin answers nothing more."""


class InvalidQuestion(TwinError):
    """The question is shorter than QUESTION_MIN or longer than QUESTION_MAX characters."""


@dataclass(frozen=True)
class Asked:
    question_id: UUID
    session_id: UUID


@dataclass(frozen=True)
class QuestionBody:
    body: str
    created_at: datetime


@dataclass(frozen=True)
class Citation:
    fact_id: UUID
    quote: str  # the cited fact's statement, as the employer reads it


@dataclass(frozen=True)
class AnswerView:
    """The Twin's latest message about the question. `citations` is filled only for an answered message."""

    answer_kind: str  # answered, unknown, declined or relayed
    body: str
    citations: tuple[Citation, ...]
    created_at: datetime
    author: Literal["twin"] = "twin"


@dataclass(frozen=True)
class ReplyView:
    body: str
    created_at: datetime


@dataclass(frozen=True)
class QuestionView:
    """One question as the caller may read it. `approval_id`, `needs_reply` and `reply` are the candidate's own view:
    the employer's view has them as None, False and None."""

    question_id: UUID
    session_id: UUID
    introduction_id: UUID
    topic: str | None  # the latest Twin message's topic; None while pending
    state: State
    question: QuestionBody
    answer: AnswerView | None
    approval_id: str | None = None
    needs_reply: bool = False
    reply: ReplyView | None = None


# ---- asking -------------------------------------------------------------------------------------------------------


def _refusal(error: DBAPIError) -> TwinError | None:
    """The intake's own name for a database refusal, or None for one that is not ours to name."""
    text_ = str(error.orig)
    sqlstate = getattr(error.orig, "sqlstate", None)
    if "consent_missing" in text_:
        return ConsentMissing("the candidate withdrew the introduction consent")
    if "twin session is closed" in text_:
        return SessionClosed("this conversation is closed")
    if sqlstate == "42501":  # row-level security: not this caller's introduction, or a token without twin:ask
        return IntroductionNotFound("no such accepted introduction")
    return None


def ask(
    conn: Connection,
    *,
    introduction_id: UUID,
    question: str,
    asked_by: UUID | None,
    agent_token_id: UUID | None,
) -> Asked:
    """Store an employer's question and queue it for the Twin. `asked_by` is the member (an organization's agent acts
    as the member who issued its token) and `agent_token_id` is set when the agent asked: both go to a session this
    call opens. Raises InvalidQuestion, IntroductionNotFound, SessionClosed, LimitReached or ConsentMissing."""
    body = question.strip()
    if not QUESTION_MIN <= len(body) <= QUESTION_MAX:
        raise InvalidQuestion(f"a question is {QUESTION_MIN} to {QUESTION_MAX} characters")
    ref = hiring.introduction_ref(conn, introduction_id)
    if ref is None or ref.status != "accepted":
        raise IntroductionNotFound("no such accepted introduction")
    # One question at a time per introduction: the session is made once and the count below is the count that counts.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:i, 0))"), {"i": str(introduction_id)})
    try:
        with conn.begin_nested():
            session_id = _open_session(
                conn, introduction_id, ref.user_id, ref.organization_id, asked_by, agent_token_id
            )
            used = conn.execute(
                text("SELECT count(*) FROM twin_messages WHERE session_id = :s AND author = 'employer'"),
                {"s": session_id},
            ).scalar_one()
            if used >= SESSION_QUESTIONS:
                raise LimitReached(f"a conversation takes {SESSION_QUESTIONS} questions")
            question_id = conn.execute(
                text(
                    """INSERT INTO twin_messages (session_id, user_id, organization_id, author, body)
                       VALUES (:s, :u, :o, 'employer', :b) RETURNING id"""
                ),
                {"s": session_id, "u": ref.user_id, "o": ref.organization_id, "b": body},
            ).scalar_one()
    except DBAPIError as error:
        named = _refusal(error)
        if named is None:
            raise
        raise named from error
    queue.enqueue(
        conn,
        RESUME,
        {"question_id": str(question_id), "cause": "start"},
        unique_key=f"{RESUME}:{question_id}:start",
        queue=QUEUE,
        max_attempts=RESUME_ATTEMPTS,
        user_id=ref.user_id,
    )
    return Asked(question_id, session_id)


def _open_session(
    conn: Connection,
    introduction_id: UUID,
    user_id: UUID,
    organization_id: UUID,
    asked_by: UUID | None,
    agent_token_id: UUID | None,
) -> UUID:
    """The introduction's open session, or a new one. SessionClosed when the only session is closed."""
    rows = conn.execute(
        text("SELECT id, status::text AS status FROM twin_sessions WHERE introduction_id = :i ORDER BY created_at, id"),
        {"i": introduction_id},
    ).all()
    for row in rows:
        if row.status == "open":
            return row.id  # type: ignore[no-any-return]
    if rows:
        raise SessionClosed("this conversation is closed")
    return conn.execute(  # type: ignore[no-any-return]
        text(
            """INSERT INTO twin_sessions (introduction_id, user_id, organization_id, opened_by, agent_token_id)
               VALUES (:i, :u, :o, :by, :t) RETURNING id"""
        ),
        {"i": introduction_id, "u": user_id, "o": organization_id, "by": asked_by, "t": agent_token_id},
    ).scalar_one()


# ---- reading ------------------------------------------------------------------------------------------------------

_QUESTION = """SELECT m.id, m.session_id, m.body, m.created_at, s.introduction_id,
                      (m.user_id = app_user_id() AND app_agent_token_id() IS NULL) AS as_candidate, m.user_id
                 FROM twin_messages m JOIN twin_sessions s ON s.id = m.session_id
                WHERE m.id = :q AND m.author = 'employer'"""

# What the Twin said about the question: a final message if there is one, else the newest holding message.
_ANSWER = """SELECT m.id, m.topic::text AS topic, m.answer_kind::text AS answer_kind, m.body, m.created_at
               FROM twin_messages m
              WHERE m.in_reply_to = :q AND m.author = 'twin'
              ORDER BY (m.answer_kind IN ('answered', 'relayed', 'declined')) DESC, m.created_at DESC, m.id DESC
              LIMIT 1"""


def question_view(conn: Connection, question_id: UUID) -> QuestionView | None:
    """The question as this connection may read it, or None when it cannot see it (another organization's, a token
    without twin:ask, an id that is no question). The citations come from `twin_message_citations`, the one function
    that lets an employer read a cited fact's statement, so no candidate table is read here for them."""
    row = conn.execute(text(_QUESTION), {"q": question_id}).one_or_none()
    if row is None:
        return None
    said = conn.execute(text(_ANSWER), {"q": question_id}).one_or_none()
    answer: AnswerView | None = None
    state: State = "pending"
    topic: str | None = None
    if said is not None:
        state = "waiting_for_candidate" if said.answer_kind == "unknown" else said.answer_kind
        topic = said.topic
        cited = (
            tuple(
                Citation(c.fact_id, c.quote)
                for c in conn.execute(text("SELECT fact_id, quote FROM twin_message_citations(:m)"), {"m": said.id})
            )
            if said.answer_kind == "answered"
            else ()
        )
        answer = AnswerView(said.answer_kind, said.body, cited, said.created_at)
    view = QuestionView(
        question_id=row.id,
        session_id=row.session_id,
        introduction_id=row.introduction_id,
        topic=topic,
        state=state,
        question=QuestionBody(row.body, row.created_at),
        answer=answer,
    )
    if not row.as_candidate:
        return view
    return _candidate_extras(conn, view, row.user_id)


def _candidate_extras(conn: Connection, view: QuestionView, user_id: UUID) -> QuestionView:
    """Add what only the candidate sees: the pending approval, whether a reply is wanted, and their own reply."""
    approval_id: str | None = None
    needs_reply = False
    if view.state == "waiting_for_candidate":
        for approval in guardian.list_approvals(conn, user_id, status="pending", limit=200):
            payload = approval["payload"] or {}
            if approval["type"] == "twin_answer" and payload.get("question_id") == str(view.question_id):
                approval_id = str(approval["approval_id"])
                needs_reply = payload.get("kind") == "relay"
                break
    reply_row = conn.execute(
        text(
            """SELECT body, created_at FROM twin_messages
                WHERE in_reply_to = :q AND author = 'candidate' ORDER BY created_at DESC, id DESC LIMIT 1"""
        ),
        {"q": view.question_id},
    ).one_or_none()
    reply = None if reply_row is None else ReplyView(reply_row.body, reply_row.created_at)
    return QuestionView(**{**view.__dict__, "approval_id": approval_id, "needs_reply": needs_reply, "reply": reply})


def wait(
    question_id: UUID,
    read: Callable[[UUID], QuestionView | None],
    seconds: float = ANSWER_WAIT_SECONDS,
) -> QuestionView | None:
    """Call `read` until the question is no longer pending or `seconds` have passed, and return the last view. `read`
    opens its own transaction each time, so a message the worker has committed since the last poll is seen. Callers are
    threadpool handlers and MCP tools running in a worker thread, so this sleeps."""
    deadline = time.monotonic() + seconds
    while True:
        view = read(question_id)
        if view is None or view.state != "pending":
            return view
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return view
        time.sleep(min(POLL_SECONDS, remaining))
