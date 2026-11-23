"""Local text embeddings (AI_SYSTEM 3.1): a small model that runs on this machine's CPU through fastembed.

It needs no key and calls no provider, so it never goes through the model gateway and writes no llm_calls row (a local
call has no cost to record). Search uses it, so search works with no model key at all.

One model serves an instance: the model's id is stored next to every vector, and changing it re-embeds every row.
"""

import logging
import os
from functools import cache
from typing import TYPE_CHECKING

from .. import log

if TYPE_CHECKING:
    from fastembed import TextEmbedding

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MODEL_ID = os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODEL
DIMENSIONS = 384  # the vector(384) columns
# The model card's instruction for short search queries. fastembed's query_embed adds nothing for this model, so the
# prefix is added here, once. Passages take no prefix.
QUERY_PREFIX = "Represent this sentence for searching relevant passages:"
BATCH_SIZE = 64  # texts per run of the model: bounds memory on a small host


class EmbeddingUnavailable(Exception):
    """The model cannot be loaded or run here. Search falls back to keywords; the embed sweep fails and is retried."""


@cache
def _model() -> "TextEmbedding | None":
    """The loaded model, or None when it cannot be loaded here.

    The first call loads it (about two seconds). A failure is remembered for the life of the process, because a missing
    model with no network can take half a minute to give up and every search would wait that long again.
    ponytail: restart to retry. Add a timed retry if the model can ever appear while the process runs.
    """
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(MODEL_ID)
        if model.embedding_size != DIMENSIONS:
            raise ValueError("the model's vectors do not fit the vector(384) columns")
        return model
    except Exception as error:  # a missing or broken model is a state to report, whatever raised it
        log.event("embed.load_failed", level=logging.WARNING, error=error)
        return None


def _loaded() -> "TextEmbedding":
    model = _model()
    if model is None:
        raise EmbeddingUnavailable(MODEL_ID)
    return model


def embed_passages(texts: list[str]) -> list[list[float]]:
    """One unit-length vector per text, in order. Texts longer than the model's 512 tokens are cut by the model."""
    if not texts:
        return []
    model = _loaded()
    try:
        return [[float(x) for x in vector] for vector in model.passage_embed(texts, batch_size=BATCH_SIZE)]
    except Exception as error:
        raise EmbeddingUnavailable(type(error).__name__) from error


def embed_query(text: str) -> list[float]:
    """The vector of a search query, with the model card's query prefix."""
    model = _loaded()
    try:
        return [float(x) for x in next(iter(model.embed([f"{QUERY_PREFIX} {text}"])))]
    except Exception as error:
        raise EmbeddingUnavailable(type(error).__name__) from error
