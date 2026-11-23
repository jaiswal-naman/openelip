"""Invented rows for the match and brief tests (plan 04-01): people, records, jobs, runs and decisions.

Everything here is made up: the candidate, the employer ("Initech Demo") and every address (example.test). The functions
write with plain SQL on the connection they are given, which is the test's own bootstrap connection (it bypasses
row-level security, and the `conn` fixture rolls its transaction back), and nothing here commits.

pytest keeps conftest.py off the import path but not this file: the tests reach it as a package under backend/,
    from tests.match.factories import make_user, make_record, make_job, make_run, make_shown_decision
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

LOCAL_DATE = date(2026, 11, 23)
RANKER_VERSION = "test-ranker-v1"
EVIDENCE_PROMPT_VERSION = "test-evidence-v1"

# One invented resume. Each fact below is a line of it, so a fact's span and quote are real positions in the text.
RESUME_TEXT = (
    "Ada Example\n"
    "Senior data engineer at Initech Demo, 2019 to 2024.\n"
    "Built nightly Python and SQL pipelines that cut report time by 40 percent.\n"
    "Led a team of three engineers.\n"
)
RESUME_FACTS = (
    ("role", "Senior data engineer at Initech Demo, 2019 to 2024."),
    ("achievement", "Built nightly Python and SQL pipelines that cut report time by 40 percent."),
    ("achievement", "Led a team of three engineers."),
)


@dataclass(frozen=True)
class Record:
    """What make_record wrote: the resume document and the three confirmed facts, in RESUME_FACTS order."""

    document_id: UUID
    fact_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class Job:
    """What make_job wrote. requirement_ids are in ordinal order, so verdicts line up with them."""

    id: int
    title: str
    company_name: str
    content_version: int
    requirement_ids: tuple[int, ...]


@dataclass(frozen=True)
class Verdict:
    """One evidence row: the verdict for a requirement, the facts it cites and an optional note."""

    verdict: str
    fact_ids: tuple[UUID, ...] = ()
    note: str | None = None


def make_user(
    conn: Connection,
    *,
    timezone: str = "Asia/Kolkata",
    frequency: str = "daily",
    weekday: int | None = None,
    email: str | None = None,
) -> UUID:
    """An active account. A weekly one gets Monday unless a weekday (1 to 7) is given, as the table requires one."""
    if frequency == "weekly" and weekday is None:
        weekday = 1
    return conn.execute(
        text(
            """INSERT INTO users (email, display_name, timezone, brief_frequency, brief_weekday)
               VALUES (:email, 'Ada Example (invented)', :tz, :frequency, :weekday) RETURNING id"""
        ),
        {
            "email": email or f"ada-{uuid4().hex[:10]}@example.test",
            "tz": timezone,
            "frequency": frequency,
            "weekday": weekday,
        },
    ).scalar_one()


def _applied_event(conn: Connection, user_id: UUID, entity: str, after: dict[str, str]) -> UUID:
    """The change-log row a constraint must point at (every non-resume row of the record does)."""
    return conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after)
               VALUES (:u, 'applied', :entity, 'create', CAST(:after AS jsonb), 'candidate_edit', 1) RETURNING id"""
        ),
        {"u": user_id, "entity": entity, "after": json.dumps(after)},
    ).scalar_one()


def make_record(conn: Connection, user_id: UUID) -> Record:
    """A confirmed Career Record: a parsed resume, three confirmed facts that quote it, a location constraint (India)
    and a remote-policy constraint (remote or hybrid), and the record header set (version 1, confirmed)."""
    document_id = conn.execute(
        text(
            """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                      extracted_text, parsed_at)
               VALUES (:u, :content, 'resume.pdf', 'application/pdf', :size, :sha, 'parsed', :body, now())
               RETURNING id"""
        ),
        {
            "u": user_id,
            "content": b"invented-file-bytes",
            "size": len(RESUME_TEXT),
            "sha": hashlib.sha256(RESUME_TEXT.encode()).digest(),
            "body": RESUME_TEXT,
        },
    ).scalar_one()
    fact_ids = []
    for kind, statement in RESUME_FACTS:
        start = RESUME_TEXT.find(statement)
        assert start >= 0, "a fact must be a line of the resume"
        fact_ids.append(
            conn.execute(
                text(
                    """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span,
                                                  source_quote, confirmed_at)
                       VALUES (:u, :kind, :statement, 'resume', :doc, int4range(:start, :stop), :statement, now())
                       RETURNING id"""
                ),
                {
                    "u": user_id,
                    "kind": kind,
                    "statement": statement,
                    "doc": document_id,
                    "start": start,
                    "stop": start + len(statement),
                },
            ).scalar_one()
        )
    conn.execute(
        text(
            """INSERT INTO constraints (user_id, kind, statement, country_codes, source, profile_event_id)
               VALUES (:u, 'location', 'Only jobs in India.', CAST(:countries AS char(2)[]), 'candidate_edit', :e)"""
        ),
        {"u": user_id, "countries": ["IN"], "e": _applied_event(conn, user_id, "constraint", {"kind": "location"})},
    )
    conn.execute(
        text(
            """INSERT INTO constraints (user_id, kind, statement, arrangements, source, profile_event_id)
               VALUES (:u, 'remote_policy', 'Remote or hybrid.', CAST(:arrangements AS work_arrangement[]),
                       'candidate_edit', :e)"""
        ),
        {
            "u": user_id,
            "arrangements": ["remote", "hybrid"],
            "e": _applied_event(conn, user_id, "constraint", {"kind": "remote_policy"}),
        },
    )
    conn.execute(text("UPDATE users SET record_version = 1, record_confirmed_at = now() WHERE id = :u"), {"u": user_id})
    return Record(document_id, tuple(fact_ids))


def make_job(
    conn: Connection,
    *,
    title: str,
    description: str,
    requirements: Sequence[tuple[str, str]],
    company_name: str = "Initech Demo",
    country: str | None = None,
    city: str | None = None,
    arrangements: Sequence[str] = (),
    employment_type: str = "unknown",
    seniority: str = "unknown",
    salary_min: int | None = None,
    salary_max: int | None = None,
    salary_currency: str | None = None,
    visa_sponsorship: str = "unknown",
    external_id: str | None = None,
) -> Job:
    """An open job at content version 1 with its requirements extracted (requirements_version 1).

    `requirements` is (text, importance) in order; each text must occur in the description, and its span is where
    str.find puts it (code points, the unit the database counts). Anything the posting does not state stays unknown.
    """
    external_id = external_id or f"fx-{uuid4().hex[:12]}"
    source_id = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', :token, :company)
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name RETURNING id"""
        ),
        {"token": company_name.lower().replace(" ", "-"), "company": company_name},
    ).scalar_one()
    job_id = conn.execute(
        text(
            """INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements,
                                 employment_type, seniority, salary_min, salary_max, salary_currency,
                                 visa_sponsorship, description, apply_url, content_hash, dedupe_key,
                                 requirements_version)
               VALUES (:source, :external, :title, :company, :country, :city, CAST(:arrangements AS work_arrangement[]),
                       :employment_type, :seniority, :salary_min, :salary_max, :salary_currency, :visa, :description,
                       'https://example.test/apply/' || :external, :hash, :external, 1)
               RETURNING id"""
        ),
        {
            "source": source_id,
            "external": external_id,
            "title": title,
            "company": company_name,
            "country": country,
            "city": city,
            "arrangements": list(arrangements),
            "employment_type": employment_type,
            "seniority": seniority,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "salary_currency": salary_currency,
            "visa": visa_sponsorship,
            "description": description,
            "hash": hashlib.sha256(description.encode()).hexdigest(),
        },
    ).scalar_one()
    requirement_ids = []
    for ordinal, (quote, importance) in enumerate(requirements, start=1):
        start = description.find(quote)
        assert start >= 0, f"requirement text is not in the description: {quote!r}"
        requirement_ids.append(
            conn.execute(
                text(
                    """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                     prompt_version)
                       VALUES (:job, 1, :ordinal, :importance, 'skill', :quote, int4range(:start, :stop),
                               'test-requirements-v1')
                       RETURNING id"""
                ),
                {
                    "job": job_id,
                    "ordinal": ordinal,
                    "importance": importance,
                    "quote": quote,
                    "start": start,
                    "stop": start + len(quote),
                },
            ).scalar_one()
        )
    return Job(job_id, title, company_name, 1, tuple(requirement_ids))


def make_run(
    conn: Connection,
    user_id: UUID,
    *,
    trigger: str = "nightly",
    local_date: date = LOCAL_DATE,
    status: str = "completed",
    evidence_prompt_version: str | None = EVIDENCE_PROMPT_VERSION,
    retrieved_count: int = 0,
    filtered_count: int = 0,
    finished_at: datetime | None = None,
) -> UUID:
    """A candidate's run on the record's current version. A run that is not running finished now unless told when;
    pass evidence_prompt_version=None for a run that did no scoring."""
    return conn.execute(
        text(
            """INSERT INTO match_runs (trigger, user_id, local_date, status, record_version, ranker_version,
                                       evidence_prompt_version, retrieved_count, filtered_count, finished_at)
               VALUES (:trigger, :u, :local_date, :status, (SELECT record_version FROM users WHERE id = :u), :ranker,
                       :prompt, :retrieved, :filtered,
                       CASE WHEN CAST(:status AS match_run_status) = 'running' THEN NULL
                            ELSE COALESCE(CAST(:finished AS timestamptz), now()) END)
               RETURNING id"""
        ),
        {
            "trigger": trigger,
            "u": user_id,
            "local_date": local_date,
            "status": status,
            "ranker": RANKER_VERSION,
            "prompt": evidence_prompt_version,
            "retrieved": retrieved_count,
            "filtered": filtered_count,
            "finished": finished_at,
        },
    ).scalar_one()


def make_decision(
    conn: Connection,
    run_id: UUID,
    user_id: UUID,
    job: Job,
    *,
    outcome: str = "withheld",
    reasons: Sequence[str] = ("not_scored",),
    local_date: date | None = None,
    retrieval_rank: int | None = None,
    violated_kinds: Sequence[str] = (),
    unverified: Sequence[str] = (),
    rank_position: int | None = None,
    summary: str | None = None,
    change_note: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    """One ledger row exactly as given, whatever its outcome. A shown one carries no evidence here (the check for that
    runs at commit, which a rolled-back test never reaches): make_shown_decision writes the evidence too."""
    return conn.execute(
        text(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, violated_kinds, unverified, retrieval_rank, rank_position,
                                            summary, change_note, created_at)
               VALUES (:run, :u, :job, :version,
                       COALESCE(CAST(:local_date AS date), (SELECT local_date FROM match_runs WHERE id = :run)),
                       :outcome, CAST(:reasons AS match_reason[]), CAST(:violated AS constraint_kind[]),
                       CAST(:unverified AS unverified_flag[]), :rank, :position, :summary, :note,
                       COALESCE(CAST(:created AS timestamptz), now()))
               RETURNING id"""
        ),
        {
            "run": run_id,
            "u": user_id,
            "job": job.id,
            "version": job.content_version,
            "local_date": local_date,
            "outcome": outcome,
            "reasons": list(reasons),
            "violated": list(violated_kinds),
            "unverified": list(unverified),
            "rank": retrieval_rank,
            "position": rank_position,
            "summary": summary,
            "note": change_note,
            "created": created_at,
        },
    ).scalar_one()


def make_shown_decision(
    conn: Connection,
    run_id: UUID,
    user_id: UUID,
    job: Job,
    verdicts: Sequence[Verdict],
    *,
    rank_position: int = 1,
    summary: str = "A fit, with the evidence below.",
    change_note: str | None = None,
    unverified: Sequence[str] = (),
) -> UUID:
    """A shown decision and one evidence row per requirement of the job, in one transaction, then the run's shown_count.

    The database's completeness check is deferred to commit; it is run here (and put back), so a decision built with
    the wrong number of verdicts fails in the factory and not in silence.
    """
    assert len(verdicts) == len(job.requirement_ids), "one verdict per requirement"
    reasons = ("top_ranked",) if change_note is None else ("top_ranked", "material_change")
    decision = make_decision(
        conn,
        run_id,
        user_id,
        job,
        outcome="shown",
        reasons=reasons,
        rank_position=rank_position,
        summary=summary,
        change_note=change_note,
        unverified=unverified,
    )
    for requirement_id, verdict in zip(job.requirement_ids, verdicts, strict=True):
        conn.execute(
            text(
                """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids, note)
                   VALUES (:decision, :u, :requirement, :verdict, CAST(:facts AS uuid[]), :note)"""
            ),
            {
                "decision": decision,
                "u": user_id,
                "requirement": requirement_id,
                "verdict": verdict.verdict,
                "facts": list(verdict.fact_ids),
                "note": verdict.note,
            },
        )
    conn.execute(
        text(
            """UPDATE match_runs SET shown_count =
                   (SELECT count(*) FROM match_decisions WHERE match_run_id = :run AND outcome = 'shown')
               WHERE id = :run"""
        ),
        {"run": run_id},
    )
    conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
    conn.execute(text("SET CONSTRAINTS match_decisions_complete DEFERRED"))
    return decision
