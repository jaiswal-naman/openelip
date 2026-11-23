"""The Twin's HTTP routes (API.md 2.12, 2.14, 3.14; TWN-01 to TWN-04):

    GET  /v1/twin/scopes      the candidate's twelve topic permissions (pay and contact details locked to never)
    PUT  /v1/twin/scopes      replace them (422 topic_locked for pay or contact details set to anything but never)
    POST /v1/twin/questions   an employer member asks the Twin inside an accepted introduction (Idempotency-Key)
    GET  /v1/twin/questions   the candidate reads every conversation about them; an employer reads one introduction's

Every handler runs on the API login for the caller and opens no worker or match transaction: the question is stored
and queued, and the Twin answers in the worker. The ask waits up to `ANSWER_WAIT_SECONDS` for the Twin's first message,
polling in fresh transactions, and then answers with the state it finds (`pending` when the worker has not posted yet).
"""

import json
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection

from app.platform import db
from app.platform.api import envelope
from app.platform.errors import ApiError
from app.platform.idempotency import Write, idempotent
from app.platform.sessions import Principal, current_user

from . import api as twin
from . import service

router = APIRouter()

Signed = Annotated[Principal, Depends(current_user)]
Idempotent = Annotated[Write, Depends(idempotent)]


def _scopes(rows: list[twin.ScopeRow]) -> dict[str, Any]:
    return {"topics": [{"topic": r.topic, "permission": r.permission, "locked": r.locked} for r in rows]}


@router.get("/v1/twin/scopes")
def get_scopes(principal: Signed) -> dict[str, Any]:
    with db.transaction(principal.user_id) as conn:
        return envelope(_scopes(twin.get_scopes(conn, principal.user_id)))


class TopicIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=40)
    permission: str = Field(min_length=1, max_length=20)


class ScopesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[TopicIn] = Field(max_length=len(twin.TOPICS))


@router.put("/v1/twin/scopes")
def put_scopes(body: ScopesIn, principal: Signed) -> dict[str, Any]:
    """Replace the permissions with the given list; a topic left out goes back to `ask_me`. Nothing is saved when any
    row is refused."""
    try:
        with db.transaction(principal.user_id) as conn:
            rows = twin.put_scopes(conn, principal.user_id, [(t.topic, t.permission) for t in body.topics])
    except twin.TopicLocked as error:
        raise ApiError(
            422, "topic_locked", "Current pay and contact details are never answered.", {"reason": str(error)}
        ) from error
    except twin.InvalidScope as error:
        raise ApiError(
            422,
            "validation_failed",
            "One or more fields are invalid.",
            {"fields": [{"field": "topics", "issue": "invalid"}]},
        ) from error
    return envelope(_scopes(rows))


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    introduction_id: UUID
    question: str = Field(min_length=twin.QUESTION_MIN, max_length=twin.QUESTION_MAX)


def _refused(error: twin.TwinError) -> ApiError:
    """The intake's refusals in the API's words."""
    if isinstance(error, twin.IntroductionNotFound):
        return ApiError(404, "not_found", "No such resource.")
    if isinstance(error, twin.SessionClosed):
        return ApiError(409, "conflict", "This conversation is closed.", {"state": "session_closed"})
    if isinstance(error, twin.LimitReached):
        return ApiError(
            409, "conflict", "This conversation has reached its question limit.", {"state": "limit_reached"}
        )
    if isinstance(error, twin.ConsentMissing):
        return ApiError(403, "consent_required", "The candidate has withdrawn their agreement to this introduction.")
    return ApiError(
        422,
        "validation_failed",
        "One or more fields are invalid.",
        {"fields": [{"field": "question", "issue": "invalid"}]},
    )


def _read(user_id: UUID) -> Callable[[UUID], twin.QuestionView | None]:
    """A reader for `twin.wait`: one fresh transaction per poll."""

    def read(question_id: UUID) -> twin.QuestionView | None:
        with db.transaction(user_id) as conn:
            return twin.question_view(conn, question_id)

    return read


@router.post("/v1/twin/questions")
def ask(body: QuestionIn, write: Idempotent) -> Response:
    """Ask the Twin a question (201). The question is stored and queued in one transaction; then the answer is awaited
    up to the wait and the view returned as it stands, `pending` when the Twin has not posted. A repeat of the same
    Idempotency-Key gets the first response as stored and waits for nothing."""

    def effect(conn: Connection) -> dict[str, Any]:
        try:
            asked = twin.ask(
                conn,
                introduction_id=body.introduction_id,
                question=body.question,
                asked_by=write.user_id,
                agent_token_id=None,
            )
        except twin.TwinError as error:
            raise _refused(error) from error
        view = twin.question_view(conn, asked.question_id)
        if view is None:  # the caller cannot read the question they just asked
            raise ApiError(404, "not_found", "No such resource.")
        return service.view_dict(view, candidate=False)

    response = write.run(effect, status=201)
    stored = json.loads(bytes(response.body))
    if stored["meta"].get("idempotent_replay") or stored["data"]["state"] != "pending":
        return response
    seen = twin.wait(UUID(stored["data"]["question_id"]), _read(write.user_id))
    if seen is None:
        return response
    stored["data"] = jsonable_encoder(service.view_dict(seen, candidate=False))
    return JSONResponse(stored, status_code=201)


@router.get("/v1/twin/questions")
def list_questions(principal: Signed, introduction_id: Annotated[UUID | None, Query()] = None) -> dict[str, Any]:
    """The candidate: every session about them, with every question, holding message, answer and their own reply. An
    employer member: the sessions of `introduction_id` as the organization may read them (no reply, no approval), and
    `422` without it."""
    with db.transaction(principal.user_id) as conn:
        if introduction_id is not None:
            return envelope({"sessions": service.list_for_employer(conn, introduction_id)})
        sessions = service.list_for_candidate(conn, principal.user_id)
        if not sessions and service.is_member_of_an_organization(conn, principal.user_id):
            raise ApiError(
                422,
                "validation_failed",
                "Give the introduction whose conversation you want.",
                {"fields": [{"field": "introduction_id", "issue": "required"}]},
            )
        return envelope({"sessions": sessions})
