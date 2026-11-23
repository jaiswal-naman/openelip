"""One attempt's invented world: a synthetic candidate with a confirmed record, an invented job, and, for an
introduction start, an organization with a role and an asked introduction (INDEX decision 9).

This is fixture tooling like app/demo.py: it writes other modules' tables directly, as the migrate role, and only on a
scratch database (every function starts by asking the server for current_database()). It uses a module's api.py where
one exists (consent through trust.api) and otherwise the insert patterns of the Phase 4 to 7 demo seeds and test
factories.
Everything comes from the scenario file; nothing is generated at random except ids. Two attempts share no row.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.hiring import share
from app.platform.llm import fake
from app.platform.sessions import seal_bytes
from app.trust import api as trust

from . import guard
from .scenario import Scenario

OPEN_TO_INTRODUCTIONS_VERSION = "open-to-introductions-2026-11"
UNDO_WINDOW_MINUTES = 1  # the database's floor (Phase 7 decision 3): the smallest real wait an approved follow-up has
TAILOR_PROMPT_VERSION = "windtunnel-tailor-v1"
RANKER_VERSION = "windtunnel-ranker-v1"
EVIDENCE_PROMPT_VERSION = "windtunnel-evidence-v1"
REQUIREMENTS_PROMPT_VERSION = "windtunnel-requirements-v1"


@dataclass(frozen=True)
class World:
    scenario_key: str
    attempt: int
    user_id: UUID
    candidate_email: str
    candidate_name: str
    job_id: int
    typed_address: str  # the employer's contact: the address the candidate types for follow-ups
    canaries: dict[str, str]
    inject: dict[str, str]
    started: datetime
    employer_id: UUID | None = None
    employer_email: str | None = None
    org_id: UUID | None = None
    role_id: UUID | None = None
    introduction_id: UUID | None = None
    share_preview_hash: str | None = None


def _text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _require_test_database(conn: Connection) -> None:
    name = conn.execute(text("SELECT current_database()")).scalar_one()
    if not guard.TEST_NAME.fullmatch(str(name)):
        raise guard.NotATestDatabase(f"refusing to write invented rows to database {name!r}")


def _person(conn: Connection, email: str, name: str, *, record: bool) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO users (email, display_name, is_synthetic, timezone, undo_window_minutes, record_version,
                                  record_confirmed_at)
               VALUES (:email, :name, true, 'UTC', :undo, CASE WHEN :record THEN 1 ELSE 0 END,
                       CASE WHEN :record THEN now() END)
               RETURNING id"""
        ),
        {"email": email, "name": name, "undo": UNDO_WINDOW_MINUTES, "record": record},
    ).scalar_one()


def _record(conn: Connection, user_id: UUID, candidate: dict[str, Any]) -> dict[str, UUID]:
    """The parsed resume, its confirmed facts (each a line of the resume) and the constraints; returns fact ids by
    their order in the scenario."""
    resume = "\n".join(candidate["resume"])
    document_id = conn.execute(
        text(
            """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                      extracted_text, parsed_at)
               VALUES (:u, :content, 'resume-invented.pdf', 'application/pdf', :size, :sha, 'parsed', :body, now())
               RETURNING id"""
        ),
        {
            "u": user_id,
            "content": seal_bytes(resume.encode()),
            "size": len(resume),
            "sha": hashlib.sha256(resume.encode()).digest(),
            "body": resume,
        },
    ).scalar_one()
    facts: dict[str, UUID] = {}
    for number, fact in enumerate(candidate["facts"]):
        statement = fact["statement"]
        start = resume.find(statement)
        if start < 0:
            raise ValueError(f"a fact of the scenario must be a line of its resume: {statement!r}")
        facts[str(number)] = conn.execute(
            text(
                """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, document_id, source_span,
                                              source_quote, confirmed_at)
                   VALUES (:u, CAST(:kind AS fact_kind), :s, CAST(:attributes AS jsonb), 'resume', :doc,
                           int4range(:start, :stop), :s, now())
                   RETURNING id"""
            ),
            {
                "u": user_id,
                "kind": fact["kind"],
                "s": statement,
                "attributes": json.dumps(fact.get("attributes", {})),
                "doc": document_id,
                "start": start,
                "stop": start + len(statement),
            },
        ).scalar_one()
    for constraint in candidate.get("constraints", []):
        event = conn.execute(
            text(
                """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source,
                                               record_version_after)
                   VALUES (:u, 'applied', 'constraint', 'create', CAST(:after AS jsonb), 'candidate_edit', 1)
                   RETURNING id"""
            ),
            {"u": user_id, "after": json.dumps({"kind": constraint["kind"]})},
        ).scalar_one()
        conn.execute(
            text(
                """INSERT INTO constraints (user_id, kind, statement, country_codes, cities, arrangements,
                                            employment_types, seniorities, salary_floor, salary_currency,
                                            company_names, source, profile_event_id)
                   VALUES (:u, CAST(:kind AS constraint_kind), :statement, CAST(:countries AS char(2)[]),
                           CAST(:cities AS text[]), CAST(:arrangements AS work_arrangement[]),
                           CAST(:employment AS employment_type[]), CAST(:seniorities AS seniority[]), :floor,
                           :currency, CAST(:companies AS text[]), 'candidate_edit', :event)"""
            ),
            {
                "u": user_id,
                "kind": constraint["kind"],
                "statement": constraint["statement"],
                "countries": constraint.get("country_codes"),
                "cities": constraint.get("cities"),
                "arrangements": constraint.get("arrangements"),
                "employment": constraint.get("employment_types"),
                "seniorities": constraint.get("seniorities"),
                "floor": constraint.get("salary_floor"),
                "currency": constraint.get("salary_currency"),
                "companies": constraint.get("company_names"),
                "event": event,
            },
        )
    return facts


def _job(
    conn: Connection,
    spec: dict[str, Any],
    *,
    tag: str,
    source: tuple[str, str, str | None],
    listed: bool,
    requirements: list[dict[str, str]],
    title: str | None = None,
    description: str | None = None,
) -> tuple[int, list[int]]:
    """A job in the index at content version 1 with its requirements; `source` is (ats, token, organization id)."""
    ats, token, organization = source
    description = description or spec["description"]
    source_id = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
               VALUES (CAST(:ats AS ats_type), :token, :company, :org, :active)
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name RETURNING id"""
        ),
        {"ats": ats, "token": token, "company": spec["company"], "org": organization, "active": ats != "direct"},
    ).scalar_one()
    job_id = conn.execute(
        text(
            """INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements,
                                 employment_type, seniority, salary_min, salary_max, salary_currency, description,
                                 apply_url, content_hash, dedupe_key, requirements_version, is_listed)
               VALUES (:source, :external, :title, :company, :country, :city, CAST(:arrangements AS work_arrangement[]),
                       CAST(:employment_type AS employment_type), CAST(:seniority AS seniority), :pay_min, :pay_max,
                       :currency, :description, :apply_url, :hash, :dedupe, 1, :listed)
               RETURNING id"""
        ),
        {
            "source": source_id,
            "external": tag,
            "title": title or spec["title"],
            "company": spec["company"],
            "country": spec.get("country"),
            "city": spec.get("city"),
            "arrangements": list(spec.get("arrangements", [])),
            "employment_type": spec.get("employment_type", "unknown"),
            "seniority": spec.get("seniority", "unknown"),
            "pay_min": spec.get("salary_min"),
            "pay_max": spec.get("salary_max"),
            "currency": spec.get("salary_currency"),
            "description": description,
            "apply_url": spec["apply_url"],
            "hash": hashlib.sha256(description.encode()).hexdigest(),
            "dedupe": f"windtunnel:{tag}",
            "listed": listed,
        },
    ).scalar_one()
    ids = []
    for ordinal, requirement in enumerate(requirements, start=1):
        start = description.find(requirement["text"])
        if start < 0:
            raise ValueError(f"a requirement must be a quote of the description: {requirement['text']!r}")
        ids.append(
            conn.execute(
                text(
                    """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                     prompt_version)
                       VALUES (:job, 1, :n, CAST(:importance AS requirement_importance), 'skill', :text,
                               int4range(:start, :stop), :prompt)
                       RETURNING id"""
                ),
                {
                    "job": job_id,
                    "n": ordinal,
                    "importance": requirement["importance"],
                    "text": requirement["text"],
                    "start": start,
                    "stop": start + len(requirement["text"]),
                    "prompt": REQUIREMENTS_PROMPT_VERSION,
                },
            ).scalar_one()
        )
    return job_id, ids


def _evidence(
    conn: Connection, decision: UUID, user_id: UUID, requirement_ids: list[int], fact_ids: list[UUID]
) -> None:
    """One evidence row per requirement, every one met by a fact of the record (cycled through the facts)."""
    for number, requirement_id in enumerate(requirement_ids):
        conn.execute(
            text(
                """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids)
                   VALUES (:d, :u, :r, 'met', CAST(:facts AS uuid[]))"""
            ),
            {"d": decision, "u": user_id, "r": requirement_id, "facts": [fact_ids[number % len(fact_ids)]]},
        )
    conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
    conn.execute(text("SET CONSTRAINTS match_decisions_complete DEFERRED"))


def _shown_match(conn: Connection, user_id: UUID, job_id: int, requirement_ids: list[int], facts: list[UUID]) -> UUID:
    """A completed match run and one shown decision for the job, with its evidence (matching has its own suites)."""
    run = conn.execute(
        text(
            """INSERT INTO match_runs (trigger, user_id, local_date, status, record_version, ranker_version,
                                       evidence_prompt_version, finished_at)
               VALUES ('nightly', :u, (now() AT TIME ZONE 'UTC')::date, 'completed', 1, :ranker, :prompt, now())
               RETURNING id"""
        ),
        {"u": user_id, "ranker": RANKER_VERSION, "prompt": EVIDENCE_PROMPT_VERSION},
    ).scalar_one()
    decision = conn.execute(
        text(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, rank_position, summary)
               VALUES (:run, :u, :job, 1, (now() AT TIME ZONE 'UTC')::date, 'shown',
                       CAST(ARRAY['top_ranked'] AS match_reason[]), 1, 'A fit, with the evidence below.')
               RETURNING id"""
        ),
        {"run": run, "u": user_id, "job": job_id},
    ).scalar_one()
    _evidence(conn, decision, user_id, requirement_ids, facts)
    conn.execute(
        text("UPDATE match_runs SET shown_count = 1 WHERE id = :run"),
        {"run": run},
    )
    return decision


def _tailored_resume(conn: Connection, user_id: UUID, job_id: int) -> UUID:
    return conn.execute(
        text(
            """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version,
                                             status, prompt_version, discarded_count)
               VALUES (:u, :j, 1, (SELECT id FROM documents WHERE user_id = :u AND status = 'parsed' LIMIT 1), 1,
                       'ready', :prompt, 0)
               RETURNING id"""
        ),
        {"u": user_id, "j": job_id, "prompt": TAILOR_PROMPT_VERSION},
    ).scalar_one()


def _introduction(
    conn: Connection, scenario: Scenario, tag: str, user: tuple[UUID, str, str], facts: list[UUID], email: str
) -> dict[str, Any]:
    """The employer, its organization and active owner, an open role with its job, a role-run decision that introduced
    the candidate with its evidence, and the asked introduction with its frozen share_preview."""
    user_id, name, candidate_email = user
    employer = scenario.world["employer"]
    owner = _person(conn, email, f"Demo Owner {_text(scenario.key)}", record=False)
    org_id = conn.execute(
        text(
            """INSERT INTO organizations (name, timezone, is_synthetic, created_by)
               VALUES (:name, 'UTC', true, :owner) RETURNING id"""
        ),
        {"name": employer["organization"], "owner": owner},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO org_members (organization_id, user_id, role, status, joined_at)
               VALUES (:o, :u, 'owner', 'active', now())"""
        ),
        {"o": org_id, "u": owner},
    )
    role_spec = employer["role"]
    job = scenario.world["job"]
    description = role_spec.get("description", job["description"])
    requirements = role_spec.get("requirements", job["requirements"])
    # The role's job carries the scenario's company address (not the app's, as a published role has), so the Researcher
    # has recorded pages to read: a deviation of fixture tooling, noted in the SUMMARY.
    job_id, job_requirements = _job(
        conn,
        {**job, "title": role_spec.get("title", job["title"])},
        tag=f"{tag}-role",
        source=("direct", str(org_id), str(org_id)),
        listed=False,
        requirements=requirements,
        description=description,
    )
    role_id = uuid4()
    pay = role_spec.get("salary_min", 1), role_spec.get("salary_max", 2), role_spec.get("salary_currency", "USD")
    conn.execute(
        text(
            """INSERT INTO roles (id, organization_id, title, description, status, salary_min, salary_max,
                                  salary_currency, requirements_confirmed_at, job_id, created_by, opened_at)
               VALUES (:id, :org, :title, :description, 'open', :lo, :hi, :currency, now(), :job, :owner, now())"""
        ),
        {
            "id": role_id,
            "org": org_id,
            "title": role_spec.get("title", job["title"]),
            "description": description,
            "lo": pay[0],
            "hi": pay[1],
            "currency": pay[2],
            "job": job_id,
            "owner": owner,
        },
    )
    for ordinal, requirement in enumerate(requirements, start=1):
        start = description.find(requirement["text"])
        conn.execute(
            text(
                """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text, span,
                                                  is_confirmed)
                   VALUES (:role, :org, :n, CAST(:importance AS requirement_importance), 'skill', :text,
                           int4range(:start, :stop), true)"""
            ),
            {
                "role": role_id,
                "org": org_id,
                "n": ordinal,
                "importance": requirement["importance"],
                "text": requirement["text"],
                "start": start,
                "stop": start + len(requirement["text"]),
            },
        )
    run = conn.execute(
        text(
            """INSERT INTO match_runs (trigger, role_id, local_date, status, ranker_version, evidence_prompt_version,
                                       started_at, finished_at)
               VALUES ('role', :role, (now() AT TIME ZONE 'UTC')::date, 'completed', :ranker, :prompt,
                       clock_timestamp(), clock_timestamp())
               RETURNING id"""
        ),
        {"role": role_id, "ranker": RANKER_VERSION, "prompt": EVIDENCE_PROMPT_VERSION},
    ).scalar_one()
    decision = conn.execute(
        text(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, summary)
               VALUES (:run, :u, :job, 1, (now() AT TIME ZONE 'UTC')::date, 'introduced',
                       CAST(ARRAY['selected_for_role'] AS match_reason[]), 'Meets every required item.')
               RETURNING id"""
        ),
        {"run": run, "u": user_id, "job": job_id},
    ).scalar_one()
    _evidence(conn, decision, user_id, job_requirements, facts)
    preview = _preview(conn, user_id, decision, name, candidate_email)
    conn.execute(text("SET LOCAL ROLE app_worker"))  # the asking job's login: the table's guards run as it
    introduction = conn.execute(
        text(
            """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview)
               VALUES (:org, :role, :u, :decision, CAST(:preview AS jsonb)) RETURNING id"""
        ),
        {"org": org_id, "role": role_id, "u": user_id, "decision": decision, "preview": json.dumps(preview)},
    ).scalar_one()
    conn.execute(text("RESET ROLE"))
    return {
        "employer_id": owner,
        "employer_email": email,
        "org_id": org_id,
        "role_id": role_id,
        "job_id": job_id,
        "introduction_id": introduction,
        "share_preview_hash": share.preview_hash(preview),
    }


def _preview(conn: Connection, user_id: UUID, decision: UUID, name: str, email: str) -> dict[str, Any]:
    """What the candidate would share on a yes, in the shape of API.md 3.12 (hiring.preview.build is the production
    builder; this builds the same shape from the same rows)."""
    quotes = dict(
        conn.execute(
            text(
                """SELECT id, coalesce(source_quote, statement) FROM profile_facts
                    WHERE user_id = :u AND confirmed_at IS NOT NULL AND rejected_at IS NULL"""
            ),
            {"u": user_id},
        ).all()
    )
    evidence = [
        {
            "requirement": row.requirement,
            "importance": row.importance,
            "verdict": row.verdict,
            "record_quotes": [quotes[fact] for fact in row.fact_ids],
        }
        for row in conn.execute(
            text(
                """SELECT r.text AS requirement, r.importance::text AS importance, e.verdict::text AS verdict,
                          e.fact_ids
                     FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
                    WHERE e.match_decision_id = :d ORDER BY r.ordinal"""
            ),
            {"d": decision},
        )
    ]
    facts = [
        {"kind": kind, "statement": statement}
        for kind, statement in conn.execute(
            text(
                """SELECT kind::text, statement FROM profile_facts
                    WHERE user_id = :u AND kind IN ('role', 'education') AND confirmed_at IS NOT NULL
                      AND rejected_at IS NULL ORDER BY created_at, id"""
            ),
            {"u": user_id},
        )
    ]
    headline = next((fact["statement"] for fact in facts if fact["kind"] == "role"), None)
    return {
        "on_accept": {"name": name, "headline": headline, "evidence": evidence, "facts": facts},
        "on_employer_yes": {"email": email},
    }


def build(conn: Connection, scenario: Scenario, attempt: int, run_tag: str) -> World:
    """Make one attempt's world and register its recordings for its candidate. `conn` is the migrate login's."""
    _require_test_database(conn)
    stem = f"wt-{_text(run_tag)}-{_text(scenario.key)}-{attempt}"
    spec = scenario.world
    candidate = spec["candidate"]
    email = f"{stem}@example.test"
    user_id = _person(conn, email, candidate["name"], record=True)
    facts = _record(conn, user_id, candidate)
    trust.grant_consent(conn, user_id, "ai_processing", trust.ai_processing_text_version())
    fact_ids = list(facts.values())
    extra: dict[str, Any] = {}
    if scenario.start == "match":
        job = spec["job"]
        job_id, requirement_ids = _job(
            conn,
            job,
            tag=stem,
            source=("lever", f"{stem}", None),
            listed=True,
            requirements=job["requirements"],
        )
        _shown_match(conn, user_id, job_id, requirement_ids, fact_ids)
        _tailored_resume(conn, user_id, job_id)
    else:
        trust.grant_consent(conn, user_id, "open_to_introductions", OPEN_TO_INTRODUCTIONS_VERSION)
        extra = _introduction(
            conn,
            scenario,
            stem,
            (user_id, candidate["name"], email),
            fact_ids,
            f"wt-{_text(run_tag)}-{attempt}-employer-{_text(scenario.key)}@example.test",
        )
        job_id = extra.pop("job_id")
    started = conn.execute(text("SELECT now()")).scalar_one()
    world = World(
        scenario_key=scenario.key,
        attempt=attempt,
        user_id=user_id,
        candidate_email=email,
        candidate_name=candidate["name"],
        job_id=job_id,
        typed_address=spec["employer"]["contact"],
        canaries=dict(spec["canaries"]),
        inject=scenario.inject,
        started=started,
        **extra,
    )
    fake.replay_for(user_id, scenario.recorded)
    return world


def release(world: World) -> None:
    """Give the candidate back to the ordinary fake provider rules."""
    fake.forget(world.user_id)
