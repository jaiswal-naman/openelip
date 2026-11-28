"""What is embedded for a job, and the sweep that fills in the vectors (AI_SYSTEM 3.2, IDX-04).

A job's vector is made once per content version and per model. New or changed text clears the vector (ingest.fetch
does that when it writes a new version), and a change of the active model (embed.active_model) makes every other
model's vector stale, so "no vector, or a vector of another model" is the one to-do list: this sweep embeds every open,
canonical, listed job on it, with the local model (no key) or OpenAI's (priced and recorded by the gateway). Each vector
is written only if the row is still the version that was read and no sweep of this model got there first, so a posting
that changes while the model is working is left for the next sweep instead of getting a vector of its old text.
"""

import time
from typing import Any

from sqlalchemy import Connection, text

from app.platform import log
from app.platform.llm.embed import active_model, embed_passages

# The part of the description that is embedded when the posting has no extracted requirements yet.
DESCRIPTION_CHARS = 2000
# Rows per sweep. One sweep every five minutes fills 6,000 rows an hour, so the backlog of a first run takes hours.
SWEEP_BATCH = 500

# No row locks: the model can take half a minute, and a fetch must not wait for it. The requirement texts are the
# current version's own (a stale version's are not used), required ones first, each group in posting order. A row with
# no vector has no model either (the table's CHECK), so "another model" covers both.
CANDIDATES = text(
    """SELECT j.id, j.content_version, j.title, left(j.description, :chars) AS description,
              coalesce((SELECT array_agg(r.text ORDER BY (r.importance = 'required') DESC, r.ordinal)
                          FROM job_requirements r
                         WHERE r.job_id = j.id AND r.content_version = j.content_version
                           AND j.requirements_version = j.content_version), '{}') AS requirements
         FROM jobs j
        WHERE j.embedding_model IS DISTINCT FROM :model
          AND j.status = 'open' AND j.canonical_job_id IS NULL AND j.is_listed
        ORDER BY j.id
        LIMIT :batch"""
)
WRITE = text(
    """UPDATE jobs SET embedding = CAST(:vector AS vector), embedding_model = :model
        WHERE id = :id AND content_version = :version AND embedding_model IS DISTINCT FROM :model"""
)


def embedding_text(title: str, description: str, requirement_texts: list[str]) -> str:
    """The title, then what the posting asks for (required first, as the caller passes it); until requirements exist,
    the title and the start of the description."""
    if requirement_texts:
        return "\n".join([title, *requirement_texts])
    return f"{title}\n{description[:DESCRIPTION_CHARS]}"


def vector_literal(vector: list[float]) -> str:
    """pgvector's text form, which the database casts with CAST(... AS vector)."""
    return "[" + ",".join(map(str, vector)) + "]"


def embed_pending(conn: Connection) -> int:
    """Embed up to SWEEP_BATCH jobs that have no vector of the active model; returns how many vectors were written.
    The model is read once and named in the embed call, which refuses if another is active by then, so a vector is
    never stored under another model's name."""
    model = active_model()
    rows = conn.execute(CANDIDATES, {"chars": DESCRIPTION_CHARS, "batch": SWEEP_BATCH, "model": model}).all()
    if not rows:
        return 0
    vectors = embed_passages(
        [embedding_text(row.title, row.description, row.requirements) for row in rows], model=model
    )
    return sum(
        conn.execute(
            WRITE, {"id": row.id, "version": row.content_version, "vector": vector_literal(vector), "model": model}
        ).rowcount
        for row, vector in zip(rows, vectors, strict=True)
    )


def embed_sweep(conn: Connection, payload: dict[str, Any]) -> None:
    """The scheduled job (ingest.embed_sweep). It raises when the model is unavailable (missing, or OpenAI failing), so
    the queue records the failure and runs it again."""
    started = time.monotonic()
    embedded = embed_pending(conn)
    log.event("ingest.embed_sweep", embedded=embedded, duration_ms=round((time.monotonic() - started) * 1000))
