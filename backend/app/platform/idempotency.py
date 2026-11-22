"""Idempotency keys on writes (plan 01-03 implements the replay; API.md 1.6, ARCHITECTURE 5.4)."""

from typing import Annotated

from fastapi import Header


def idempotent(idempotency_key: Annotated[str | None, Header()] = None) -> None:
    """FastAPI dependency every write route declares."""
    raise NotImplementedError("plan 01-03")
