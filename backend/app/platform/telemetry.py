"""Request spans and the /metrics endpoint (plan 01-06 implements it; ARCHITECTURE 9.1)."""

from fastapi import FastAPI


def instrument(app: FastAPI) -> None:
    """Does nothing until plan 01-06."""
