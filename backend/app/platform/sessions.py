"""The signed-in person for a request (plan 01-03 implements this; ARCHITECTURE 6.2)."""

from dataclasses import dataclass
from uuid import UUID

from .errors import ApiError


@dataclass(frozen=True)
class Principal:
    user_id: UUID


def current_user() -> Principal:
    """FastAPI dependency for a route that needs a signed-in person.

    Until plan 01-03 nobody can be signed in, so it refuses: a route that depends on it is closed, not open.
    """
    raise ApiError(401, "unauthenticated", "Sign in to continue.")
