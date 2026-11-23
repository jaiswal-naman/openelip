"""Chat sessions, messages and replies (plan 05-04; API.md 2.8 and 4.2; CHT-01 to CHT-04; decisions 1 to 6 and 24).

A message is accepted in one transaction: the checks, then the candidate's row and the assistant row (empty, with no
finish_reason, so the 202 can name it). The reply is written by a background thread of this process (`_generate`) that
starts after that transaction commits, whether or not a stream is open. The thread runs the turn loop with the seven
tools, appends every event to the reply hub (hub.py, what a stream reads), writes a row for each tool result as it
arrives, and at the end stores the text, the citations, the proposals and how the reply ended, then sends message.end.

Rows of a session are ordered by created_at: the candidate's message, its tool rows, then the assistant row, whose
created_at is set again when the reply ends (it was made first, at acceptance, so the 202 could name it).

A reply left without a finish_reason by a process that is gone (nothing in the hub is running it) can never finish. It
is ended as `interrupted` the next time anything reads it, or its person posts again.
"""

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID

from fastapi.responses import Response
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict
from sqlalchemy import Connection, Row, func, insert, text

from app.ingest import api as ingest
from app.platform import config, db, log
from app.platform.errors import INTERNAL_ERROR, ApiError
from app.platform.idempotency import Write
from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid, registry
from app.platform.llm.gateway import usd
from app.trust import api as trust

from . import hub, loop, tools
from .models import ChatSessionKind, FinishReason, chat_messages, chat_sessions
from .refs import AliasTable

MAX_TEXT = 4000  # characters in one message (API.md 2.8, the plan)
CHAT_MESSAGES_PER_HOUR = 30  # API.md 1.8
CHAT_HISTORY_MESSAGES = 20  # assumption: the earlier user and assistant messages the model is shown with a new one
# ponytail: no paging. A person has a handful of sessions, and a session a few dozen messages (30 an hour is the most
# they can send); past these the newest are listed. A cursor (API.md 1.5) is the upgrade when anyone gets there.
LIST_LIMIT = 100
TRANSCRIPT_LIMIT = 500
# message.end of a reply that was ended by reading it has no event number to give: the process that wrote it is gone.
# This one is above any a real reply reaches, so a client that reconnects with it (and only then) is sent 204.
ORPHAN_END_ID = 1_000_000

UNTRUSTED: JsonDict = {"x-untrusted-text": True}  # API.md 1.10: model text may carry what a posting said

TASKS_OF_A_REPLY = ("chat.turn", "record.propose")  # the calls a reply's usage and cost add up (decision 24)


# ---- what the routes return ----------------------------------------------------------------------------------------


class SessionView(BaseModel):
    id: UUID
    kind: ChatSessionKind
    created_at: datetime
    last_message_at: datetime | None


class CitationView(BaseModel):
    """A ledger row a reply rests on: the alias its text carries as [[d1]], and the real id of that kind."""

    alias: str
    kind: Literal["decision", "job", "fact", "constraint"]
    decision_id: UUID | None = None
    job_id: int | None = None
    fact_id: UUID | None = None
    constraint_id: UUID | None = None


class ProposalView(BaseModel):
    """A record change the agent proposed. It is only a proposal until the candidate confirms event_id in the app."""

    event_id: UUID
    entity: str
    operation: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    statement: str


class MessageView(BaseModel):
    id: UUID
    role: Literal["user", "assistant", "tool"]
    text: str | None = Field(json_schema_extra=UNTRUSTED)  # null for a tool row; "" while the reply is being written
    tool_name: str | None
    citations: list[CitationView]
    proposals: list[ProposalView]
    finish_reason: FinishReason | None  # null while the reply is being written, and for rows that are not replies
    created_at: datetime


class Accepted(BaseModel):
    user_message_id: UUID
    message_id: UUID
    status: Literal["running"]
    stream_url: str


class ReplyState(BaseModel):
    message_id: UUID
    finish_reason: FinishReason | None  # null: still being written (a stop was asked for and has not ended it yet)


def not_found() -> ApiError:
    """The answer for anything that is not the caller's, or does not exist: the same, so ids reveal nothing."""
    return ApiError(404, "not_found", "No such resource.")


def _message_view(row: Row[Any]) -> MessageView:
    refs = row.refs or {}
    return MessageView(
        id=row.id,
        role=row.role,
        text=row.content,
        tool_name=row.tool_name,
        citations=[CitationView.model_validate(c) for c in refs.get("citations", [])],
        proposals=[ProposalView.model_validate(p) for p in refs.get("proposals", [])],
        finish_reason=row.finish_reason,
        created_at=row.created_at,
    )


# ---- sessions and the transcript -----------------------------------------------------------------------------------


def create_session(conn: Connection, user_id: UUID, kind: ChatSessionKind) -> SessionView:
    row = conn.execute(
        insert(chat_sessions)
        .values(user_id=user_id, kind=kind)
        .returning(chat_sessions.c.id, chat_sessions.c.created_at, chat_sessions.c.last_message_at)
    ).one()
    return SessionView(id=row.id, kind=kind, created_at=row.created_at, last_message_at=row.last_message_at)


def list_sessions(user_id: UUID) -> list[SessionView]:
    """The person's sessions, the most recently active first (a session with no message counts from its creation)."""
    with db.transaction(user_id) as conn:
        rows = conn.execute(
            text(
                """SELECT id, kind::text AS kind, created_at, last_message_at FROM chat_sessions
                    WHERE user_id = :u ORDER BY COALESCE(last_message_at, created_at) DESC, id DESC LIMIT :n"""
            ),
            {"u": user_id, "n": LIST_LIMIT},
        ).all()
    return [SessionView.model_validate(row, from_attributes=True) for row in rows]


def _own_session(conn: Connection, user_id: UUID, session_id: UUID) -> None:
    owned = conn.execute(
        text("SELECT 1 FROM chat_sessions WHERE id = :s AND user_id = :u"), {"s": session_id, "u": user_id}
    ).first()
    if owned is None:
        raise not_found()


def transcript(user_id: UUID, session_id: UUID) -> list[MessageView]:
    """The session's messages in order: the candidate's, the agent's tool rows, the replies. The latest
    TRANSCRIPT_LIMIT when there are more."""
    with db.transaction(user_id) as conn:
        _own_session(conn, user_id, session_id)
        _settle(conn, user_id, session_id=session_id)
        rows = conn.execute(
            text(
                """SELECT * FROM (
                       SELECT id, role::text AS role, content, tool_name, refs, finish_reason, created_at
                         FROM chat_messages WHERE session_id = :s AND user_id = :u
                        ORDER BY created_at DESC, id DESC LIMIT :n) latest
                    ORDER BY created_at, id"""
            ),
            {"s": session_id, "u": user_id, "n": TRANSCRIPT_LIMIT},
        ).all()
    return [_message_view(row) for row in rows]


# ---- replies a process left behind ---------------------------------------------------------------------------------


def _settle(
    conn: Connection, user_id: UUID, *, session_id: UUID | None = None, message_id: UUID | None = None
) -> list[UUID]:
    """End as `interrupted` every unfinished reply of the person (of this session, or this one reply) that this process
    is not running, and return the ids of those it is. A running reply is left alone."""
    sql = "SELECT id FROM chat_messages WHERE user_id = :u AND role = 'assistant' AND finish_reason IS NULL"
    params: dict[str, Any] = {"u": user_id}
    if session_id is not None:
        sql, params["s"] = sql + " AND session_id = :s", session_id
    if message_id is not None:
        sql, params["m"] = sql + " AND id = :m", message_id
    still_running = []
    for unfinished in conn.execute(text(sql), params).scalars().all():
        if hub.is_running(unfinished):
            still_running.append(unfinished)
            continue
        conn.execute(
            text(
                """UPDATE chat_messages
                      SET finish_reason = 'interrupted',
                          refs = refs || jsonb_build_object('last_event_id', CAST(:end AS integer))
                    WHERE id = :id AND user_id = :u AND finish_reason IS NULL"""
            ),
            {"id": unfinished, "u": user_id, "end": ORPHAN_END_ID},
        )
        log.event("chat.reply_settled", message_id=unfinished, outcome="interrupted")
    return still_running


@dataclass(frozen=True)
class StoredReply:
    """A reply as the database holds it: what a stream is built from when the hub no longer has the reply."""

    id: UUID
    finish_reason: FinishReason | None
    content: str
    refs: dict[str, Any]


def get_reply(user_id: UUID, session_id: UUID, message_id: UUID) -> StoredReply:
    """The reply `message_id` of this person's session (404 for anything else, including a candidate's own message),
    after a reply that nothing is writing has been ended as interrupted."""
    with db.transaction(user_id) as conn:
        return _reply_row(conn, user_id, session_id, message_id)


def _reply_row(conn: Connection, user_id: UUID, session_id: UUID, message_id: UUID) -> StoredReply:
    _settle(conn, user_id, session_id=session_id, message_id=message_id)
    row = conn.execute(
        text(
            """SELECT id, finish_reason, content, refs FROM chat_messages
                WHERE id = :m AND session_id = :s AND user_id = :u AND role = 'assistant'"""
        ),
        {"m": message_id, "s": session_id, "u": user_id},
    ).one_or_none()
    if row is None:
        raise not_found()
    return StoredReply(row.id, row.finish_reason, row.content or "", row.refs or {})


def stop(write: Write, session_id: UUID, message_id: UUID) -> Response:
    """Ask the reply to stop (200). The loop looks before every model turn and tool call, so it ends as `stopped` at the
    next one; a reply that has already ended is left as it is."""

    def effect(conn: Connection) -> ReplyState:
        found = _reply_row(conn, write.user_id, session_id, message_id)
        reply = hub.get(message_id)
        if reply is not None and not reply.finished.is_set():
            reply.request_stop()
        return ReplyState(message_id=message_id, finish_reason=found.finish_reason)

    return write.run(effect)


# ---- accepting a message -------------------------------------------------------------------------------------------

_RECENT_MESSAGES = text(
    """SELECT count(*) AS n,
              CAST(ceil(extract(epoch FROM min(created_at) + interval '1 hour' - now())) AS integer) AS wait
         FROM chat_messages WHERE user_id = :u AND role = 'user' AND created_at > now() - interval '1 hour'"""
)
_TOUCH_SESSION = text("UPDATE chat_sessions SET last_message_at = clock_timestamp() WHERE id = :s AND user_id = :u")


@dataclass(frozen=True)
class _Generation:
    """Everything the thread needs once its message is committed."""

    reply: hub.Reply
    user_message_id: UUID
    sent_at: datetime  # the candidate's row: the history is what came before it
    text: str
    context_job_id: int | None

    @property
    def session_id(self) -> UUID:
        return self.reply.session_id

    @property
    def user_id(self) -> UUID:
        return self.reply.user_id


def _check_rate(conn: Connection, user_id: UUID) -> None:
    """429 at CHAT_MESSAGES_PER_HOUR messages in the last hour, counted from the rows (API.md 1.8). The wait is the time
    until the oldest of them is an hour old."""
    recent = conn.execute(_RECENT_MESSAGES, {"u": user_id}).one()
    if recent.n >= CHAT_MESSAGES_PER_HOUR:
        raise ApiError(
            429,
            "rate_limited",
            f"You can send {CHAT_MESSAGES_PER_HOUR} messages an hour. Try again later.",
            {"retry_after_seconds": max(1, recent.wait)},
        )


def accept(
    conn: Connection, user_id: UUID, session_id: UUID, words: str, context_job_id: int | None
) -> tuple[Accepted, _Generation]:
    """The checks, in order, then both rows and the reply's place in the hub. The text length was checked with the body.

        404  the session is not the person's        403  no ai_processing consent (or the wording changed)
        503  no model could answer chat.turn        429  30 messages in the last hour
        409  a reply of theirs is still running     404  the job on screen is not in the index

    All of it runs inside the idempotent write, so a retry of an accepted message gets the first answer back and none of
    these checks (a running reply of its own would be a 409) is applied to it."""
    _own_session(conn, user_id, session_id)
    trust.require_consent(conn, user_id, "ai_processing")
    registry.route(loop.TASK, config.get())  # raises AIDisabled (503 ai_disabled) with nothing to call; calls no model
    # One message at a time per person: the count and the insert must not interleave, or parallel posts would all see
    # 29 and all be kept, and two replies could start. The lock is held until this transaction ends.
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"chat.post:{user_id}"})
    _check_rate(conn, user_id)
    if _settle(conn, user_id):
        raise ApiError(
            409, "conflict", "A reply is still being written. Wait for it, or stop it.", {"state": "reply_running"}
        )
    if context_job_id is not None and ingest.get_job(conn, context_job_id) is None:
        raise not_found()
    user_row = conn.execute(
        insert(chat_messages)
        .values(session_id=session_id, user_id=user_id, role="user", content=words, created_at=func.clock_timestamp())
        .returning(chat_messages.c.id, chat_messages.c.created_at)
    ).one()
    message_id: UUID = conn.execute(
        insert(chat_messages)
        .values(session_id=session_id, user_id=user_id, role="assistant", content="", created_at=func.clock_timestamp())
        .returning(chat_messages.c.id)
    ).scalar_one()
    conn.execute(_TOUCH_SESSION, {"s": session_id, "u": user_id})
    reply = hub.start(message_id, session_id, user_id)  # before the commit: nobody can ask for the id until it returns
    accepted = Accepted(
        user_message_id=user_row.id,
        message_id=message_id,
        status="running",
        stream_url=f"/v1/chat/sessions/{session_id}/messages/{message_id}/stream",
    )
    return accepted, _Generation(reply, user_row.id, user_row.created_at, words, context_job_id)


def send(write: Write, session_id: UUID, words: str, context_job_id: int | None) -> Response:
    """Accept a message (202) and, once that is committed, start writing the reply. A repeat of the same Idempotency-Key
    gets the first response and starts nothing."""
    created: list[_Generation] = []

    def effect(conn: Connection) -> Accepted:
        accepted, generation = accept(conn, write.user_id, session_id, words, context_job_id)
        created.append(generation)
        return accepted

    try:
        response = write.run(effect, status=202)
    except BaseException:
        for generation in created:  # the transaction did not commit: there is no reply to write
            hub.discard(generation.reply.message_id)
        raise
    for generation in created:
        threading.Thread(
            target=_generate, args=(generation,), name=f"chat-reply-{generation.reply.message_id}", daemon=True
        ).start()
    return response


# ---- writing the reply ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Spent:
    input_tokens: int
    output_tokens: int
    micro_usd: int


def _spent(user_id: UUID, since: datetime) -> _Spent:
    """What this person's chat calls have cost since the reply began: the ledger rows of the two tasks a reply makes.
    Tailoring and packet calls the worker makes for the same person meanwhile are left out (decision 24)."""
    with db.transaction(user_id) as conn:
        row = conn.execute(
            text(
                """SELECT COALESCE(SUM(input_tokens), 0) AS i, COALESCE(SUM(output_tokens), 0) AS o,
                          COALESCE(SUM(cost_microusd), 0) AS c
                     FROM llm_calls WHERE user_id = :u AND task = ANY(:tasks) AND created_at >= :since"""
            ),
            {"u": user_id, "tasks": list(TASKS_OF_A_REPLY), "since": since},
        ).one()
    return _Spent(int(row.i), int(row.o), int(row.c))


def _history(conn: Connection, job: _Generation) -> list[dict[str, str]]:
    """The last CHAT_HISTORY_MESSAGES user and assistant messages before this one, oldest first."""
    rows = conn.execute(
        text(
            """SELECT role::text AS role, content FROM chat_messages
                WHERE session_id = :s AND user_id = :u AND role IN ('user', 'assistant') AND created_at < :before
                ORDER BY created_at DESC, id DESC LIMIT :n"""
        ),
        {"s": job.session_id, "u": job.user_id, "before": job.sent_at, "n": CHAT_HISTORY_MESSAGES},
    ).all()
    return [{"role": row.role, "content": row.content or ""} for row in reversed(rows)]


def _alias_table(conn: Connection, job: _Generation) -> AliasTable:
    """The session's aliases, rebuilt from what was stored with its tool rows and replies."""
    rows = conn.execute(
        text(
            """SELECT refs -> 'aliases' AS aliases FROM chat_messages
                WHERE session_id = :s AND user_id = :u AND role IN ('tool', 'assistant')
                  AND refs -> 'aliases' IS NOT NULL
                ORDER BY created_at, id"""
        ),
        {"s": job.session_id, "u": job.user_id},
    ).all()
    return AliasTable.from_tool_rows([{"aliases": row.aliases} for row in rows])


def _store_tool_row(job: _Generation, tool: str, aliases: dict[str, Any]) -> None:
    """One row per tool result, as it arrives: the tool's name and the aliases it gave out, so that the next message can
    rebuild them. No result text is kept."""
    with db.transaction(job.user_id) as conn:
        conn.execute(
            insert(chat_messages).values(
                session_id=job.session_id,
                user_id=job.user_id,
                role="tool",
                tool_name=tool,
                refs={"reply_id": str(job.reply.message_id), "aliases": aliases},
                created_at=func.clock_timestamp(),
            )
        )


def _watched(tool: tools.Tool, failures: list[BaseException]) -> tools.Tool:
    """The tool, remembering what it raised: the loop ends the turn on any exception and keeps only its class."""

    def call(user_id: UUID, args: Any, aliases: AliasTable) -> Any:
        try:
            return tool(user_id, args, aliases)
        except Exception as error:
            failures.append(error)
            raise

    return call


@dataclass(frozen=True)
class _Outcome:
    result: loop.TurnResult | None  # None when the turn itself raised
    error: BaseException | None  # what to tell the client, when the reply failed
    started: datetime  # the database's clock when the reply began
    extra_aliases: dict[str, Any]  # aliases this reply made that no tool row holds (the job on screen)


def _run(job: _Generation) -> _Outcome:
    reply = job.reply
    with db.transaction(job.user_id) as conn:
        started: datetime = conn.execute(text("SELECT clock_timestamp()")).scalar_one()
        history, table = _history(conn, job), _alias_table(conn, job)
    stored = set(table.to_refs())  # what an earlier message already stored
    failures: list[BaseException] = []

    def emit(name: str, data: dict[str, Any]) -> None:
        if name == "tool.result":
            try:
                _store_tool_row(job, data["tool"], data["aliases"])
            except Exception as error:  # the turn ends: a tool result that cannot be kept cannot be cited later
                failures.append(error)
                raise
            stored.update(data["aliases"])
            data = {key: value for key, value in data.items() if key != "aliases"}  # a stream carries ids, not aliases
        reply.emit(name, data)

    def tokens_used() -> int:
        spent = _spent(job.user_id, started)
        return spent.input_tokens + spent.output_tokens

    # ponytail: "today" is the UTC date (TurnContext.today is left unset): no module's api.py gives chat the candidate's
    # time zone (match.ledger.today_in is not exported). East of UTC, the early hours name yesterday to the model. Pass
    # today=<the candidate's local date> here once an api.py exports it.
    context = loop.TurnContext(job.user_id, job.text, history, table, job.context_job_id)
    watched = {name: _watched(tool, failures) for name, tool in tools.build_tools(job.text).items()}
    try:
        result = loop.run_turn(context, watched, emit=emit, should_stop=reply.stopping, tokens_used=tokens_used)
    except Exception as error:
        # the gateway's AIDisabled, ProviderUnavailable or SchemaInvalid, or something unforeseen
        return _Outcome(None, error, started, {})
    extra = {alias: ref for alias, ref in table.to_refs().items() if alias not in stored}
    return _Outcome(result, failures[-1] if failures else None, started, extra)


def _error_event(error: BaseException) -> dict[str, Any]:
    """The `error` event of a reply that failed (API.md 4.2, 1.11): a code and a message the person may read. Never the
    cause's own text, which could hold what was said or what a model answered."""
    if isinstance(error, AIDisabled | ProviderUnavailable):
        return {"code": error.code, "message": error.message, "is_retryable": error.code == "dependency_unavailable"}
    if isinstance(error, SchemaInvalid):
        return {
            "code": "dependency_unavailable",
            "message": "The AI model's answer could not be used. Try again.",
            "is_retryable": True,
        }
    code, message = INTERNAL_ERROR
    log.event("chat.reply_failed", level=logging.ERROR, error=error)  # the class name only
    return {"code": code, "message": message, "is_retryable": False}


def _conclude(job: _Generation, outcome: _Outcome, begun: float) -> None:
    """Store how the reply ended, then tell the stream. message.end is sent last, after the row is written, so a client
    that sees it can read the finished reply."""
    reply, result = job.reply, outcome.result
    if outcome.error is not None:
        reply.emit("error", _error_event(outcome.error))
    finish = cast(FinishReason, result.finish_reason) if result is not None else "interrupted"
    words = result.text if result is not None else ""
    spent = _spent(job.user_id, outcome.started)
    usage = {"input_tokens": spent.input_tokens, "output_tokens": spent.output_tokens}
    events = reply.since(0)
    citations = [event.data for event in events if event.name == "citation"]
    proposals = [event.data for event in events if event.name == "proposal"]
    refs: dict[str, Any] = {
        "citations": citations,
        "proposals": proposals,
        "proposal_event_ids": [proposal["event_id"] for proposal in proposals],
        "unknown_citations": result.unknown_citations if result is not None else 0,
        "last_event_id": reply.last_id + 1,  # message.end, below
        "usage": usage,
        "cost_usd": usd(spent.micro_usd),
    }
    if outcome.extra_aliases:
        refs["aliases"] = outcome.extra_aliases
    with db.transaction(job.user_id) as conn:
        conn.execute(
            text(
                """UPDATE chat_messages
                      SET content = :content, refs = CAST(:refs AS jsonb), finish_reason = :finish,
                          created_at = clock_timestamp()
                    WHERE id = :id AND user_id = :u"""
            ),
            {
                "content": words,
                "refs": json.dumps(refs, ensure_ascii=False),
                "finish": finish,
                "id": reply.message_id,
                "u": job.user_id,
            },
        )
        conn.execute(_TOUCH_SESSION, {"s": job.session_id, "u": job.user_id})
    log.event(
        "chat.reply",
        message_id=reply.message_id,
        outcome=finish,
        input_tokens=spent.input_tokens,
        output_tokens=spent.output_tokens,
        cost_microusd=spent.micro_usd,
        citations=len(citations),
        proposals=len(proposals),
        duration_ms=round((time.monotonic() - begun) * 1000),
    )
    reply.emit(
        "message.end",
        {
            "message_id": str(reply.message_id),
            "finish_reason": finish,
            "usage": usage,
            "cost_usd": usd(spent.micro_usd),
        },
    )


def _generate(job: _Generation) -> None:
    """The body of the reply thread. However it goes, the reply ends: message.end is sent and the hub entry is marked
    finished, so no stream waits for it for ever."""
    reply, begun = job.reply, time.monotonic()
    try:
        _conclude(job, _run(job), begun)
    except Exception as error:  # the database failed while reading or storing: the row will read as interrupted
        log.event("chat.store_failed", level=logging.ERROR, error=error)
        reply.emit("error", _error_event(error))
        end = {"message_id": str(reply.message_id), "finish_reason": "interrupted", "usage": None, "cost_usd": None}
        reply.emit("message.end", end)
    finally:
        reply.finish()
