"""Sign-in with an emailed code, sessions and the account (plan 01-03 implements the bodies; API.md 1.2 and 2.2).

The routes and their Pydantic models are the contract: the web types are generated from them, so plan 01-03 fills the
bodies and keeps the models.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .api import Envelope
from .errors import ApiError

router = APIRouter(prefix="/v1")

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"  # a plausible address; the database checks for an @ as well


class CodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)


class CodeRequested(BaseModel):
    """The same for every address, so the endpoint does not reveal whether an account exists."""

    expires_in_seconds: int


class SessionRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=EMAIL_PATTERN)
    code: str = Field(pattern=r"^\d{6}$")
    timezone: str | None = None  # an IANA name; used only when this sign-in creates the account


class Me(BaseModel):
    id: UUID
    email: str
    timezone: str
    created_at: datetime


@router.post("/auth/codes", status_code=202, response_model=Envelope[CodeRequested])
def request_code(body: CodeRequest) -> dict[str, Any]:
    raise ApiError(501, "not_implemented", "Not implemented yet.")


@router.post("/auth/sessions", status_code=201, response_model=Envelope[Me])
def create_session(body: SessionRequest) -> dict[str, Any]:
    raise ApiError(501, "not_implemented", "Not implemented yet.")


@router.delete("/auth/sessions/current", status_code=204)
def end_session() -> Response:
    raise ApiError(501, "not_implemented", "Not implemented yet.")


@router.get("/me", response_model=Envelope[Me])
def me() -> dict[str, Any]:
    raise ApiError(501, "not_implemented", "Not implemented yet.")
