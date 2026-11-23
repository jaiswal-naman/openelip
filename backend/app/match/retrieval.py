"""Constraint-first hybrid retrieval for one candidate (DATA_MODEL 6.1, AI_SYSTEM 3.3; MAT-01, MAT-07).

The person's hard constraints are compiled and applied in the database before any ranking (the `eligible` set), so a job
that breaks one is never ranked, scored or shown, and an approximate index can never drop a row the filter kept. Two
ranked lists are taken over that set, the best keyword matches and the nearest vectors, and fused by reciprocal rank:
a job's score is the sum of 1 / (60 + rank) over the lists it is in.

    candidates       the eligible jobs, best first, each with the fields the checker and the ranker read
    filtered_sample  the violators ranked the same way, for the "why not" view; filtered_count counts every violator
    retrieve         candidates for the person's own record (what plan 04-07's benchmark and other modules call)
    profile_text, query_terms, profile_vector, active_preferences   what is searched for, built from the record

Every statement runs on the connection it is given. The run passes an `app_match` connection, which may read the job
index (DATA_MODEL 6.1 does the same), the constraints, the usable facts and the preferences, and nothing else.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, TextClause, text

from app.platform import log
from app.platform.llm.embed import MODEL_ID, EmbeddingUnavailable, embed_passages
from app.profile import api as profile

from .checker import JobFacts
from .evidence import REMOVED, _clean  # the contact-detail scrubber of the evidence request, one copy of it

RRF_K = 60  # the conventional constant of reciprocal rank fusion
LIST_DEPTH = 200  # rows each list keeps before fusion (DATA_MODEL 6.1)
RUN_LIMIT = 50  # jobs a run takes from the fused list (DATA_MODEL 6.1)
FILTERED_SAMPLE = 20  # assumption: violators stored per run so "why not" has rows; the count is of all of them
MAX_TERMS = 30  # assumption: keywords searched for, so a long skills list cannot swamp the query
MODES = ("hybrid", "keyword", "vector")
# Most telling first: the embedding model reads only the first 512 tokens of the passage.
_KIND_ORDER = ("role", "skill", "achievement", "project", "education", "certification", "other")
_WORD = re.compile(r"[^\W_]+")  # letters and digits of any script: no quote, dash or operator survives

_OPEN = "j.status = 'open' AND j.canonical_job_id IS NULL AND j.is_listed"
# MAT-07: never the same content version twice. A violator is judged on the constraints alone.
_NOT_SHOWN = """NOT EXISTS (SELECT 1 FROM match_decisions d WHERE d.user_id = :u AND d.job_id = j.id
                              AND d.job_content_version = j.content_version AND d.outcome = 'shown')"""
_LISTS = {
    "dense": """dense AS (
    SELECT id, row_number() OVER (ORDER BY distance, id) AS r
      FROM (SELECT e.id, e.embedding <=> CAST(:vector AS vector) AS distance
              FROM eligible e WHERE e.embedding IS NOT NULL AND e.embedding_model = :model
             ORDER BY distance, e.id LIMIT :depth) nearest
)""",
    "sparse": """sparse AS (
    SELECT id, row_number() OVER (ORDER BY relevance DESC, id) AS r
      FROM (SELECT e.id, ts_rank_cd(e.search, tsq) AS relevance
              FROM eligible e, websearch_to_tsquery('english', :terms) AS tsq
             WHERE e.search @@ tsq
             ORDER BY relevance DESC, e.id LIMIT :depth) best
)""",
}
# Enum arrays are read as text[]: the driver returns an array of a custom enum as one string otherwise.
_COLUMNS = """j.id AS job_id, j.title, j.company_name, j.country, j.city,
       j.work_arrangements::text[] AS work_arrangements, j.employment_type::text AS employment_type,
       j.seniority::text AS seniority, j.salary_min, j.salary_max, j.salary_currency,
       j.visa_sponsorship::text AS visa_sponsorship, j.content_version, j.requirements_version,
       j.materially_changed_at AS changed_at, j.change_note,
       unverified_fields(j, c.cc)::text[] AS unverified, constraint_violations(j, c.cc)::text[] AS violated,
       row_number() OVER (ORDER BY fused.score DESC, j.id) AS retrieval_rank, fused.score AS rrf_score"""


@dataclass(frozen=True)
class Candidate:
    """A job the retrieval returned: what the checker reads (`facts`), what the ranker reads, and where it ranked.
    `violated_kinds` is empty for an eligible job and names the constraints a filtered one broke."""

    job_id: int
    title: str
    facts: JobFacts
    content_version: int
    requirements_version: int | None  # equal to content_version once the job's requirements are extracted
    changed_at: datetime  # jobs.materially_changed_at
    change_note: str | None  # what ingest says changed in this version, None at first sight
    unverified: tuple[str, ...]  # constrained fields the posting does not state
    violated_kinds: tuple[str, ...]
    retrieval_rank: int
    rrf_score: float


# ---- what is searched for ------------------------------------------------------------------------------------------


def active_preferences(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """The person's active soft preferences as {kind, value, weight}, oldest first.

    ponytail: a read of the preferences table, because profile.api offers no read of them that the match role can run
    (get_record also reads the change log, which that role may not). The match role holds SELECT on this table for
    exactly this (DATA_MODEL 5.6). Move it behind profile.api when that module gains a preferences read."""
    rows = conn.execute(
        text(
            """SELECT kind::text AS kind, value, weight FROM preferences
               WHERE user_id = :u AND superseded_at IS NULL ORDER BY created_at, id"""
        ),
        {"u": user_id},
    )
    return [{"kind": row.kind, "value": row.value, "weight": row.weight} for row in rows]


def _plain(statement: str) -> str:
    """The statement on one line with any email, link or phone number taken out (it is embedded locally, but the
    passage carries no contact detail anywhere)."""
    return " ".join(_clean(statement, ()).replace(REMOVED, "").split())


def profile_text(facts: Sequence[Mapping[str, Any]], preferences: Sequence[Mapping[str, Any]]) -> str:
    """The usable facts and the things the person wants (positive preferences) as one passage, most telling first.
    Empty when there is nothing to say."""
    wanted = [_plain(p["value"]) for p in preferences if p["weight"] > 0]
    lines = [f"Looking for: {'; '.join(wanted)}."] if wanted else []
    ordered = sorted(
        facts, key=lambda f: _KIND_ORDER.index(f["kind"]) if f["kind"] in _KIND_ORDER else len(_KIND_ORDER)
    )
    return "\n".join([*lines, *filter(None, (_plain(f["statement"]) for f in ordered))])


def query_terms(facts: Sequence[Mapping[str, Any]], preferences: Sequence[Mapping[str, Any]]) -> str:
    """The words of the target titles the person wants, then of their skill facts, OR-joined for the keyword list, at
    most MAX_TERMS of them. Every word is letters and digits alone, so nothing the person wrote can act as a
    search operator ("or" itself is dropped for the same reason)."""
    sources = [p["value"] for p in preferences if p["kind"] == "target_title" and p["weight"] > 0]
    sources += [f["statement"] for f in facts if f["kind"] == "skill"]
    words = dict.fromkeys(w for source in sources for w in _WORD.findall(source.lower()) if w != "or")
    return " or ".join(list(words)[:MAX_TERMS])


def profile_vector(facts: Sequence[Mapping[str, Any]], preferences: Sequence[Mapping[str, Any]]) -> list[float] | None:
    """The passage embedded on this machine (nothing is stored), or None when there is no passage or the embedder
    cannot run here, in which case retrieval is by keywords alone."""
    passage = profile_text(facts, preferences)
    if not passage:
        return None
    try:
        return embed_passages([passage])[0]
    except EmbeddingUnavailable:
        log.event("match.keyword_fallback", level=logging.WARNING)
        return None


# ---- retrieval -----------------------------------------------------------------------------------------------------


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text form, which the database casts with CAST(... AS vector) (as ingest does, one line)."""
    return "[" + ",".join(map(str, vector)) + "]"


def _statement(*, violators: bool, dense: bool, sparse: bool) -> TextClause:
    """DATA_MODEL 6.1: the eligible set, the lists asked for, reciprocal rank fusion, then the typed columns of the top
    rows. With `violators` the eligibility test is inverted: the jobs the constraints removed, ranked the same way."""
    broken = "constraint_violations(j, c.cc) <> '{}'"
    kept = f"constraint_violations(j, c.cc) = '{{}}' AND {_NOT_SHOWN}"
    lists = [name for name, used in (("dense", dense), ("sparse", sparse)) if used]
    both = " UNION ALL ".join(f"SELECT id, r FROM {name}" for name in lists)
    ctes = [
        "c AS (SELECT compile_constraints(:u) AS cc)",
        f"""eligible AS MATERIALIZED (SELECT j.id, j.search, j.embedding, j.embedding_model FROM jobs j, c
                                      WHERE {_OPEN} AND {broken if violators else kept})""",
        *(_LISTS[name] for name in lists),
        f"fused AS (SELECT id, sum(1.0::float8 / ({RRF_K} + r)) AS score FROM ({both}) ranked GROUP BY id)",
    ]
    return text(
        f"""WITH {", ".join(ctes)}
            SELECT {_COLUMNS} FROM fused JOIN jobs j ON j.id = fused.id, c
            ORDER BY fused.score DESC, j.id LIMIT :limit"""
    )


def _candidate(row: Row[Any]) -> Candidate:
    facts = JobFacts(
        company_name=row.company_name,
        country=row.country,
        city=row.city,
        work_arrangements=tuple(row.work_arrangements),
        employment_type=row.employment_type,
        seniority=row.seniority,
        salary_min=row.salary_min,
        salary_max=row.salary_max,
        salary_currency=row.salary_currency,
        visa_sponsorship=row.visa_sponsorship,
    )
    return Candidate(
        job_id=row.job_id,
        title=row.title,
        facts=facts,
        content_version=row.content_version,
        requirements_version=row.requirements_version,
        changed_at=row.changed_at,
        change_note=row.change_note,
        unverified=tuple(row.unverified),
        violated_kinds=tuple(row.violated),
        retrieval_rank=row.retrieval_rank,
        rrf_score=float(row.rrf_score),
    )


def _fused(
    conn: Connection,
    user_id: UUID,
    *,
    violators: bool,
    mode: str,
    limit: int,
    depth: int,
    vector: Sequence[float] | None,
    terms: str,
) -> list[Candidate]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    dense = mode in ("hybrid", "vector") and vector is not None  # hybrid with no vector is the keyword list alone
    sparse = mode in ("hybrid", "keyword") and bool(terms.strip())
    if not (dense or sparse):
        return []  # nothing to rank on: unknown stays unknown, no job is picked for no reason
    values: dict[str, Any] = {"u": user_id, "limit": limit, "depth": depth}
    if dense and vector is not None:
        values |= {"vector": _vector_literal(vector), "model": MODEL_ID}
    if sparse:
        values["terms"] = terms
    rows = conn.execute(_statement(violators=violators, dense=dense, sparse=sparse), values)
    return [_candidate(row) for row in rows]


def candidates(
    conn: Connection,
    user_id: UUID,
    *,
    mode: str = "hybrid",
    limit: int = RUN_LIMIT,
    depth: int = LIST_DEPTH,
    vector: Sequence[float] | None = None,
    terms: str = "",
) -> list[Candidate]:
    """The open, canonical, listed jobs that break none of the person's hard constraints and were not shown to them at
    the job's current content version, best first (score, then job id), at most `limit` of them.

    mode "keyword" uses the keyword list alone, "vector" the nearest-vector list alone, "hybrid" both. The vector is
    the caller's (profile_vector); without one, or when no job has a vector of the current model, hybrid is the keyword
    list. A job that states a field a constraint covers, and contradicts it, is never here; one that leaves the field
    unstated is, with the field named in `unverified`."""
    return _fused(conn, user_id, violators=False, mode=mode, limit=limit, depth=depth, vector=vector, terms=terms)


def filtered_sample(
    conn: Connection,
    user_id: UUID,
    *,
    vector: Sequence[float] | None,
    terms: str,
    limit: int = FILTERED_SAMPLE,
) -> list[Candidate]:
    """The jobs the constraints removed, best ranked first by the same fusion, each with the kinds it broke: the rows
    "why not" can show. At most `limit`; filtered_count has every one."""
    return _fused(
        conn, user_id, violators=True, mode="hybrid", limit=limit, depth=LIST_DEPTH, vector=vector, terms=terms
    )


def filtered_count(conn: Connection, user_id: UUID) -> int:
    """Every open, canonical, listed job that breaks one of the person's hard constraints, stored or not."""
    found = conn.execute(
        text(
            f"""WITH c AS (SELECT compile_constraints(:u) AS cc)
                SELECT count(*) FROM jobs j, c WHERE {_OPEN} AND constraint_violations(j, c.cc) <> '{{}}'"""
        ),
        {"u": user_id},
    ).scalar_one()
    return int(found)


def retrieve(
    conn: Connection, user_id: UUID, *, mode: str = "hybrid", limit: int = RUN_LIMIT, depth: int = LIST_DEPTH
) -> list[Candidate]:
    """candidates() for the person's own record: the keywords and the profile vector are built from their usable facts
    and preferences. This is the match.api.retrieve of the Phase 4 contract."""
    facts = profile.usable_facts(conn, user_id)
    preferences = active_preferences(conn, user_id)
    vector = profile_vector(facts, preferences) if mode != "keyword" else None
    terms = query_terms(facts, preferences)
    return candidates(conn, user_id, mode=mode, limit=limit, depth=depth, vector=vector, terms=terms)
