"""What this installation sends to whom (plan 01-04 implements the body; API.md 2.20, ARCHITECTURE 8.5).

The models are the contract: the web types are generated from them.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.platform.api import Envelope
from app.platform.errors import ApiError

router = APIRouter(prefix="/v1/privacy")


class Subprocessor(BaseModel):
    name: str
    kind: str  # "model_provider" or "email"
    purpose: str
    receives: str  # in plain words, what is sent to it
    tasks: list[str]  # the model tasks it serves; empty for an email host


class Subprocessors(BaseModel):
    items: list[Subprocessor]
    outside_copies_statement: str  # copies made outside the system cannot be reached by deletion


@router.get("/subprocessors", response_model=Envelope[Subprocessors])
def subprocessors() -> dict[str, Any]:
    raise ApiError(501, "not_implemented", "Not implemented yet.")
