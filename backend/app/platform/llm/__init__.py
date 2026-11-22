"""The model gateway (plan 01-04 implements it; ARCHITECTURE 5.8, AI_SYSTEM 2).

Callers name a task and get a validated result back. Routing, retries and the cost ledger live behind complete().
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from ..errors import ApiError


class AIDisabled(ApiError):
    """No usable provider is configured for the task. It is an ApiError, so the API answers it as 503 ai_disabled."""

    def __init__(self) -> None:
        super().__init__(503, "ai_disabled", "This feature needs an AI model and none is configured.")


def complete(
    task: str,
    schema: type[BaseModel],
    messages: list[dict[str, Any]],
    *,
    principal: UUID | None,
    prompt_version: str,
) -> BaseModel:
    """Until plan 01-04 no provider is configured for any task."""
    raise AIDisabled
