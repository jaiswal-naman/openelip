"""What this installation sends to whom (API.md 2.20, ARCHITECTURE 8.5). Built from the running configuration on every
request, never hard-coded: it changes when config/models.toml or the environment changes. Public.

The models are the contract: the web types are generated from them.
"""

from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import BaseModel

from app.platform import config
from app.platform.api import Envelope, envelope
from app.platform.llm import registry

router = APIRouter(prefix="/v1/privacy")

OUTSIDE_COPIES = (
    "Copies made outside this system, for example an email that has already been delivered to someone else, "
    "cannot be reached by deletion."
)
# What a task of each data class sends (AI_SYSTEM 2.6). A class missing here fails the test that covers them all.
RECEIVES = {
    "public": "Public job text.",
    "de-identified": "Candidate facts with name, email, phone and links removed.",
    "identified": "Resume lines and chat messages, which can identify a person.",
    "synthetic": "Invented test messages only.",
}


class Subprocessor(BaseModel):
    name: str
    kind: str  # "model_provider" or "email"
    purpose: str
    receives: str  # in plain words, what is sent to it
    tasks: list[str]  # the model tasks it serves; empty for an email host


class Subprocessors(BaseModel):
    items: list[Subprocessor]
    outside_copies_statement: str  # copies made outside the system cannot be reached by deletion


def _model_providers(settings: config.Settings) -> list[Subprocessor]:
    """Only providers a task would be sent to right now: with no key, nothing leaves, so nothing is listed."""
    by_provider: dict[str, list[registry.Route]] = {}
    for route in registry.active_routes(settings):
        by_provider.setdefault(route.provider, []).append(route)
    items = []
    for routes in by_provider.values():
        classes = dict.fromkeys(r.data_class for r in routes)
        items.append(
            Subprocessor(
                name=routes[0].label,
                kind="model_provider",
                purpose=" ".join(r.description for r in routes),
                receives=" ".join([*(RECEIVES[c] for c in classes), routes[0].retention_note]),
                tasks=[r.task for r in routes],
            )
        )
    return sorted(items, key=lambda item: item.name)


def _email_host(settings: config.Settings) -> Subprocessor:
    host = urlsplit(settings.smtp_url).hostname or "Not stated by the operator"  # never the credentials in the URL
    return Subprocessor(
        name=host,
        kind="email",
        purpose="Delivers the sign-in codes this installation emails.",
        receives="Email addresses and the sign-in codes sent to them.",
        tasks=[],
    )


@router.get("/subprocessors", response_model=Envelope[Subprocessors])
def subprocessors() -> dict[str, Any]:
    settings = config.get()
    items = [*_model_providers(settings), _email_host(settings)]
    return envelope(Subprocessors(items=items, outside_copies_statement=OUTSIDE_COPIES))
