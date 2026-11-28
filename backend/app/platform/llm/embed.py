"""Text embeddings (AI_SYSTEM 3.1): OpenAI's text-embedding-3-large when the platform runs on an OpenAI key, a small
model on this machine's CPU through fastembed otherwise.

Which one is active is a function of the settings (active_model), not a constant: EMBEDDING_PROVIDER=auto follows the
model provider, local always takes the local model, openai always takes OpenAI's. The local model needs no key and
calls no provider, so it writes no llm_calls row; OpenAI's goes through the model gateway, which prices and records
every request. Search uses whichever is active, and works with no model key at all (keywords, then the local model).

One model serves an instance at a time: the model's id is stored next to every vector, and a change of model re-embeds
every row. Callers that store or rank vectors pass the id they read (`model`); an embedder that is running another model
by then refuses, so a vector is never stored under another model's name.
"""

import logging
import math
import os
from functools import cache
from typing import TYPE_CHECKING

from .. import config, log
from ..config import Role
from . import gateway, registry
from .failures import AIDisabled, ProviderUnavailable

# ONNX Runtime (under fastembed) sends usage events to Microsoft unless this is set before it loads. Nothing here may
# leave the computer, so it is set on import, ahead of the lazy fastembed import below. An explicit setting wins.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")

if TYPE_CHECKING:
    from uuid import UUID

    from fastembed import TextEmbedding

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
LOCAL_MODEL_ID = os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL
DIMENSIONS = 384  # the local model's vectors
# The id stored beside an OpenAI vector: provider, model and the length asked of the API. It is repeated in the
# migration that indexes it (db/versions/e1_embedding_models.py), and the length in config/models.toml.
OPENAI_MODEL_ID = "openai/text-embedding-3-large@1536"
OPENAI_DIMENSIONS = 1536
UNIT_TOLERANCE = 1e-3  # how far from length 1 an OpenAI vector may be before it is scaled
# The model card's instruction for short search queries. fastembed's query_embed adds nothing for this model, so the
# prefix is added here, once. Passages take no prefix. OpenAI's models are not trained on it, so they get none.
QUERY_PREFIX = "Represent this sentence for searching relevant passages:"
BATCH_SIZE = 64  # texts per run of the local model: bounds memory on a small host


class EmbeddingUnavailable(Exception):
    """The active model cannot be loaded or run here. Search falls back to keywords; the sweep fails and is retried."""


def _openai_route() -> registry.Route | None:
    """The embed route when it is OpenAI itself. A model server of your own speaks the same protocol but has no embed
    route, and the recorded fake is no embedder, so neither is ever taken for it."""
    try:
        found = registry.route(gateway.EMBED_TASK, config.get())
    except AIDisabled:
        return None
    return found if found.provider == "openai" and not found.is_local else None


def _selected() -> tuple[str, registry.Route | None]:
    """(the active model's id, the OpenAI route that serves it, which is None for the local model and when OpenAI was
    asked for and there is no key to serve it)."""
    setting = config.get().embedding_provider
    route = None if setting == "local" else _openai_route()
    if route is not None or setting == "openai":
        return OPENAI_MODEL_ID, route
    return LOCAL_MODEL_ID, None


def active_model() -> str:
    """The id of the model that makes vectors now. Rows and queries are compared only within one id."""
    return _selected()[0]


@cache
def _model() -> "TextEmbedding | None":
    """The loaded local model, or None when it cannot be loaded here.

    The first call loads it (about two seconds). A failure is remembered for the life of the process, because a missing
    model with no network can take half a minute to give up and every search would wait that long again.
    ponytail: restart to retry. Add a timed retry if the model can ever appear while the process runs.
    """
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(LOCAL_MODEL_ID)
        if model.embedding_size != DIMENSIONS:
            raise ValueError("the model's vectors do not fit the 384-dimension index")
        return model
    except Exception as error:  # a missing or broken model is a state to report, whatever raised it
        log.event("embed.load_failed", level=logging.WARNING, error=error)
        return None


def _loaded() -> "TextEmbedding":
    model = _model()
    if model is None:
        raise EmbeddingUnavailable(LOCAL_MODEL_ID)
    return model


def _unit(vector: list[float]) -> list[float]:
    """The vector as it came when it is unit length, scaled to it when it is not (cosine ranking does not need it; the
    convention that every stored vector has length 1 does). A zero vector is no embedding."""
    length = math.sqrt(sum(x * x for x in vector))
    if length == 0:
        raise EmbeddingUnavailable("zero vector")
    return vector if abs(length - 1.0) <= UNIT_TOLERANCE else [x / length for x in vector]


def _from_openai(
    texts: list[str], route: registry.Route | None, principal: "UUID | None", ledger: Role, quick: bool
) -> list[list[float]]:
    """The vectors from the gateway, checked: one per text, 1536 wide, unit length. Any failure is `unavailable`, so a
    caller handles OpenAI as it handles a local model that will not load."""
    if route is None:
        raise EmbeddingUnavailable("no OpenAI route")
    if ledger == "api" and principal is None:  # llm_calls takes an API login's row only as a signed-in person's own
        raise EmbeddingUnavailable("no signed-in person to record the call against")
    try:
        vectors = gateway.embed_texts(texts, principal=principal, ledger=ledger, quick=quick)
    except (AIDisabled, ProviderUnavailable) as error:
        raise EmbeddingUnavailable(type(error).__name__) from error
    if len(vectors) != len(texts) or any(len(v) != OPENAI_DIMENSIONS for v in vectors):
        raise EmbeddingUnavailable("the vectors do not fit the 1536-dimension index")
    return [_unit(v) for v in vectors]


def _expect(model: str | None, selected: str) -> None:
    if model is not None and model != selected:
        raise EmbeddingUnavailable("the embedding model changed")


def embed_passages(
    texts: list[str], *, model: str | None = None, principal: "UUID | None" = None, ledger: Role = "worker"
) -> list[list[float]]:
    """One unit-length vector per text, in order. `model` is the id the caller read from active_model(): the call is
    refused if the active model is another by now. `principal` and `ledger` are for OpenAI's ledger row (whose call it
    is, and which database login writes it); the local model has none. Texts longer than the local model's 512 tokens
    are cut by the model."""
    if not texts:
        return []
    selected, route = _selected()
    _expect(model, selected)
    if selected == OPENAI_MODEL_ID:
        return _from_openai(texts, route, principal, ledger, quick=False)
    local = _loaded()
    try:
        return [[float(x) for x in vector] for vector in local.passage_embed(texts, batch_size=BATCH_SIZE)]
    except Exception as error:
        raise EmbeddingUnavailable(type(error).__name__) from error


def embed_query(
    text: str, *, model: str | None = None, principal: "UUID | None" = None, ledger: Role = "api"
) -> list[float]:
    """The vector of a search query: with the model card's prefix for the local model, as it is for OpenAI's, whose
    request is short and never retried (a person is waiting). Arguments as in embed_passages."""
    selected, route = _selected()
    _expect(model, selected)
    if selected == OPENAI_MODEL_ID:
        return _from_openai([text], route, principal, ledger, quick=True)[0]
    local = _loaded()
    try:
        return [float(x) for x in next(iter(local.embed([f"{QUERY_PREFIX} {text}"])))]
    except Exception as error:
        raise EmbeddingUnavailable(type(error).__name__) from error
