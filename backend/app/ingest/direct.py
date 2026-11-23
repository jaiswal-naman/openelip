"""Employer roles in the job index: an unlisted job per published role (plan 06-03, DATA_MODEL 11 issue 3).

A role an organization publishes becomes one `jobs` row on its `direct` source, so the hiring desk reuses requirement
evidence, the constraint filter, the decision ledger and the citation trigger unchanged. The job is never
listed (`is_listed = false`): search, matches and extraction skip it, and candidates reach the role only through its
introduction. The source is created with `is_active = false`, which is what the index fetch selects on, so the fetch
never visits it (there is no feed to read).

The API login cannot write `jobs`, so the rows are written by SECURITY DEFINER functions that check the caller is a
member of the role's organization (db/sql/p6_0601_publish_close.sql); the API process never holds the worker's login.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import config

from .normalize import content_hash

PROMPT_VERSION = "employer-confirmed"  # the requirement rows were confirmed by a person, not read by a model


@dataclass(frozen=True)
class DirectRequirement:
    """One confirmed requirement of a role, with its span in the job description (code points, end exclusive)."""

    ordinal: int
    importance: str
    kind: str
    text: str
    start: int
    end: int
    min_years: int | None = None


def publish_direct_job(
    conn: Connection, *, role_id: UUID, title: str, description: str, requirements: Sequence[DirectRequirement]
) -> int:
    """Create the role's unlisted job and its requirement rows and return the job id.

    The rows are written by the database function `direct_job_open` (db/sql/p6_0601_publish_close.sql), which the API
    login may call: it takes the role's own title, company, place, pay and terms from the `roles` row, refuses a caller
    who is not an active member of the role's organization, and refuses a role that is not a draft or already has a job.
    One `direct` source per organization is created on first use and reused after. The job is open, at content version 1
    with its requirements complete for that version. `title` must be the role's title (it goes into the content hash).
    Every requirement's text must be the description at its span; the database trigger refuses the insert otherwise.
    """
    rows = [
        {
            "ordinal": item.ordinal,
            "importance": item.importance,
            "kind": item.kind,
            "text": item.text,
            "start": item.start,
            "end": item.end,
            "min_years": item.min_years,
        }
        for item in requirements
    ]
    job_id: int = conn.execute(
        text("SELECT direct_job_open(:role, :description, CAST(:requirements AS jsonb), :apply_url, :hash, :prompt)"),
        {
            "role": role_id,
            "description": description,
            "requirements": json.dumps(rows),
            # Candidates reach a role only through its introduction, so the apply link is the app, not a posting page.
            "apply_url": f"{config.get().app_base_url}/app/today",
            "hash": content_hash(title, description, None),  # the rule Phase 2's store uses for every posting
            "prompt": PROMPT_VERSION,
        },
    ).scalar_one()
    return job_id


def close_direct_job(conn: Connection, job_id: int) -> None:
    """Close the job of a closed role, through `direct_job_close` (a member of the role's organization only). Closing
    twice changes nothing."""
    conn.execute(text("SELECT direct_job_close(:id)"), {"id": job_id})
