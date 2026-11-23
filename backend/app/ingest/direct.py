"""Employer roles in the job index: an unlisted job per published role (plan 06-03, DATA_MODEL 11 issue 3).

A role an organization publishes becomes one `jobs` row on that organization's `direct` source, so the hiring desk reuses
requirement evidence, the constraint filter, the decision ledger and the citation trigger unchanged. The job is never
listed (`is_listed = false`): search, matches and extraction skip it, and candidates reach the role only through its
introduction. The source is created with `is_active = false`, which is what the index fetch selects on, so the fetch
never visits it (there is no feed to read).

These are plain functions over a worker connection (the API role cannot write `jobs`). The caller has already checked
that the person may publish; nothing here knows about people.
"""

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
    conn: Connection,
    *,
    organization_id: UUID,
    company_name: str,
    role_id: UUID,
    title: str,
    description: str,
    country: str | None,
    city: str | None,
    work_arrangements: Sequence[str],
    employment_type: str,
    seniority: str,
    salary_min: int,
    salary_max: int,
    salary_currency: str,
    visa_sponsorship: str,
    requirements: Sequence[DirectRequirement],
) -> int:
    """Create the role's unlisted job and its requirement rows and return the job id.

    One `direct` source per organization (token = the organization id) is created on first use and reused after. The job
    is open, at content version 1 with its requirements complete for that version. Every requirement's text must be the
    description at its span; the database trigger refuses the insert otherwise.
    """
    source_id = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
               VALUES ('direct', :token, :company, :org, false)
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name
               RETURNING id"""
        ),
        {"token": str(organization_id), "company": company_name, "org": organization_id},
    ).scalar_one()
    job_id: int = conn.execute(
        text(
            """INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements,
                                 employment_type, seniority, salary_min, salary_max, salary_currency, visa_sponsorship,
                                 description, apply_url, content_hash, dedupe_key, content_version,
                                 requirements_version, is_listed)
               VALUES (:source, :external, :title, :company, :country, :city, CAST(:arrangements AS work_arrangement[]),
                       CAST(:employment_type AS employment_type), CAST(:seniority AS seniority), :pay_min, :pay_max,
                       :currency, CAST(:visa AS visa_sponsorship), :description, :apply_url, :hash, :dedupe, 1, 1,
                       false)
               RETURNING id"""
        ),
        {
            "source": source_id,
            "external": str(role_id),
            "title": title,
            "company": company_name,
            "country": country,
            "city": city,
            "arrangements": list(work_arrangements),
            "employment_type": employment_type,
            "seniority": seniority,
            "pay_min": salary_min,
            "pay_max": salary_max,
            "currency": salary_currency,
            "visa": visa_sponsorship,
            "description": description,
            # Candidates reach a role only through its introduction, so the apply link is the app, not a posting page.
            "apply_url": f"{config.get().app_base_url}/app/today",
            "hash": content_hash(title, description, None),  # the rule Phase 2's store uses for every posting
            "dedupe": f"direct:{role_id}",  # one per role, so the duplicate fold never merges two roles
        },
    ).scalar_one()
    for item in requirements:
        conn.execute(
            text(
                """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                 min_years, prompt_version)
                   VALUES (:job, 1, :ordinal, CAST(:importance AS requirement_importance),
                           CAST(:kind AS requirement_kind), :text, int4range(:start, :end), :min_years, :prompt)"""
            ),
            {
                "job": job_id,
                "ordinal": item.ordinal,
                "importance": item.importance,
                "kind": item.kind,
                "text": item.text,
                "start": item.start,
                "end": item.end,
                "min_years": item.min_years,
                "prompt": PROMPT_VERSION,
            },
        )
    return job_id


def close_direct_job(conn: Connection, job_id: int) -> None:
    """Close the job of a closed role. Closing twice changes nothing."""
    conn.execute(
        text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :id AND status = 'open'"),
        {"id": job_id},
    )
