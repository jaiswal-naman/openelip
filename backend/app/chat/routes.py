"""HTTP routes of the chat module (API.md 2.8: sessions, the transcript, messages and stop; the reply stream is in
stream.py's route below).

    POST /v1/chat/sessions                                         open a session (`open` or `onboarding`)
    GET  /v1/chat/sessions                                         the person's sessions, most recently active first
    GET  /v1/chat/sessions/{id}/messages                           the transcript, with citations and proposals
    POST /v1/chat/sessions/{id}/messages                           send a message: 202, the reply is written meanwhile
    POST /v1/chat/sessions/{id}/messages/{reply_id}/stop           stop a running reply

The person is the session's, never a field of a path or a body (`extra="forbid"` refuses one): a session, message or
reply that is not theirs is a 404, the same as one that does not exist. Writes take an Idempotency-Key (API.md 1.6).
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection

from app.platform.api import Envelope, envelope
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import service
from .models import ChatSessionKind

router = APIRouter()

MAX_JOB_ID = 2**63 - 1  # job ids are bigints: a larger number cannot be one

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ChatSessionKind = "open"


class MessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=service.MAX_TEXT)
    context_job_id: int | None = Field(default=None, ge=1, le=MAX_JOB_ID)  # the job page the candidate has open

    @field_validator("text")
    @classmethod
    def _text_that_can_be_stored(cls, value: str) -> str:
        """A message with words in it, and with nothing the database cannot hold: PostgreSQL text has no NUL, and a lone
        surrogate (valid JSON) cannot be encoded. Either would be a 500 at the insert."""
        if not value.strip():
            raise ValueError("write a message")
        if "\x00" in value:
            raise ValueError("the message has a character that cannot be stored")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("the message is not valid text") from None
        return value


@router.post("/v1/chat/sessions", status_code=201, response_model=Envelope[service.SessionView])
def open_chat_session(body: SessionIn, write: Idempotent) -> Response:
    """Open a session. `onboarding` is accepted and stored and runs the same agent (decision 22)."""

    def effect(conn: Connection) -> service.SessionView:
        return service.create_session(conn, write.user_id, body.kind)

    return write.run(effect, status=201)


@router.get("/v1/chat/sessions", response_model=Envelope[list[service.SessionView]])
def list_chat_sessions(principal: Signed) -> dict[str, Any]:
    return envelope(service.list_sessions(principal.user_id))


@router.get("/v1/chat/sessions/{session_id}/messages", response_model=Envelope[list[service.MessageView]])
def chat_transcript(session_id: UUID, principal: Signed) -> dict[str, Any]:
    """The person's messages in order: their own, the agent's tool rows and its replies, each reply with the ledger rows
    it cites and the record changes it proposed. `finish_reason` is null while a reply is still being written."""
    return envelope(service.transcript(principal.user_id, session_id))


@router.post("/v1/chat/sessions/{session_id}/messages", status_code=202, response_model=Envelope[service.Accepted])
def send_chat_message(session_id: UUID, body: MessageIn, write: Idempotent) -> Response:
    """Send a message. 202 with the reply's id and where to stream it; the reply is written whether or not anyone
    listens. 403 without the ai_processing consent, 503 with no model, 429 past 30 messages an hour, 409 while a
    reply of theirs is still being written."""
    return service.send(write, session_id, body.text, body.context_job_id)


@router.post("/v1/chat/sessions/{session_id}/messages/{message_id}/stop", response_model=Envelope[service.ReplyState])
def stop_chat_reply(session_id: UUID, message_id: UUID, write: Idempotent) -> Response:
    """Stop a reply: it ends as `stopped` at the next model turn or tool call. A reply that has ended stays as it is."""
    return service.stop(write, session_id, message_id)
