"""Scheduled jobs (plan 01-02 implements it; ARCHITECTURE 5.3)."""

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Connection

F = TypeVar("F", bound=Callable[..., Any])


def every(name: str, seconds: int) -> Callable[[F], F]:
    """Decorator that registers a job to run once per period."""
    raise NotImplementedError("plan 01-02")


def tick(conn: Connection, now: datetime) -> int:
    """Enqueue every registered job whose current slot has none; returns how many were enqueued."""
    raise NotImplementedError("plan 01-02")
