"""Invented rows for the hiring-desk tests of the later plans (06-02): organizations, members, roles, candidates,
role-run decisions, asks and acceptances, each built so that every database guard accepts it.

    from tests.hiring.factories import (
        make_org, make_member, make_role, make_open_role, make_candidate, make_introduced_decision, make_ask,
        make_accepted,
    )

    org = make_org(conn)                          # "Halden Demo" and its owner
    role = make_open_role(conn, org)              # published as an unlisted direct job, three requirements
    candidate = make_candidate(conn)              # a confirmed record, open to introductions, AI processing agreed
    ask = make_ask(conn, role, candidate)         # an introduction in status asked, with its frozen share_preview
    accepted = make_accepted(conn, role, candidate)   # accepted: scoped consent, due time, shortlist entry

Everything here is made up: organizations end in "Demo" (a synthetic one must), every address is example.test (unless a
test names another), people are called "Demo ...", and the postings and resume are the ones of tests/match/factories.py.

How to use them:
  * Every factory starts by checking that the connection is on a database whose name ends in _test, and refuses it
    otherwise, so invented rows can never reach database `app`.
  * They write with plain SQL on the connection they are given: the test's own bootstrap connection (the `conn`
    fixture, whose transaction is rolled back, or `engine.begin()` for an API test that commits). Build the rows first
    and call `act_as` afterwards: a factory refuses a connection that already dropped to another role, because under
    row-level security its own reads would come back empty.
  * The guards of the schema still run. make_ask inserts as `app_worker`, as the ask job will, so the grant, the policy
    and introductions_guard all apply; a candidate who is not open to introductions, who blocked the organization, or an
    organization with an overdue answer makes it raise, and a test can show that on purpose.
  * make_candidate agrees to AI processing under the wording in force when it runs (the provider list is part of that
    wording). A test that changes LLM_PROVIDER should do so before it builds candidates.
  * Times that a test may need to order (membership, runs, shortlist entries) come from clock_timestamp(), so rows
    made one after another in one transaction are in that order.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Row, text

from app.platform import config
from app.trust import api as trust
from tests.match.factories import (
    EVIDENCE_PROMPT_VERSION,
    RANKER_VERSION,
    Job,
    Record,
    Verdict,
    make_decision,
    make_record,
    make_user,
)

# A role with two required items and one preferred one, quoted from its own text so that every span resolves. The
# required ones are what the invented resume of tests/match/factories.py supports ("Python and SQL pipelines"); the
# preferred one it does not mention.
DESCRIPTION = (
    "Halden Demo is hiring a data engineer.\n"
    "What you'll need\n"
    "Python is required.\n"
    "SQL is required.\n"
    "Experience with Airflow is a plus.\n"
)
REQUIREMENTS = (
    ("Python is required.", "required"),
    ("SQL is required.", "required"),
    ("Experience with Airflow is a plus.", "preferred"),
)
PAY = (80_000, 120_000, "USD")  # salary_min, salary_max, salary_currency
CONSENT_TEXT = "test-v1"  # the wording of the consents a candidate gives in onboarding and settings, invented


@dataclass(frozen=True)
class Org:
    id: UUID
    name: str
    owner: UUID  # an active member with the role owner


@dataclass(frozen=True)
class Member:
    id: UUID  # the org_members row: what accept_org_invitation and the member routes take
    org: UUID
    user: UUID | None  # None for a pending invitation
    email: str
    role: str
    status: str  # active or invited


@dataclass(frozen=True)
class Role:
    id: UUID
    org: UUID
    title: str
    description: str
    company: str  # the organization's name, which is the job's company name
    requirement_ids: tuple[UUID, ...]  # role_requirements, in ordinal order
    job_id: int | None = None  # the unlisted job of an open role
    job_requirement_ids: tuple[int, ...] = ()  # that job's job_requirements, in ordinal order


@dataclass(frozen=True)
class Candidate:
    id: UUID
    email: str
    name: str
    record: Record  # the resume document and the three confirmed facts


@dataclass(frozen=True)
class Decision:
    id: UUID
    run: UUID  # the role run it belongs to: pass it to the next decision to put two candidates in one run
    role: UUID
    user: UUID
    outcome: str  # introduced or withheld


@dataclass(frozen=True)
class Ask:
    id: UUID  # the introduction
    org: UUID
    role: UUID
    user: UUID
    decision: UUID
    asked_at: datetime
    expires_at: datetime
    preview: dict[str, Any] = field(hash=False)  # the share_preview exactly as stored


@dataclass(frozen=True)
class Accepted:
    ask: Ask
    consent: UUID  # the introduction consent, scoped to this organization and role
    shortlist: UUID  # the shortlist entry
    answer_due_at: datetime

    @property
    def id(self) -> UUID:
        return self.ask.id


def _require_test_database(conn: Connection) -> None:
    """The first line of every factory (T-06-45)."""
    name = conn.execute(text("SELECT current_database()")).scalar_one()
    if not name.endswith("_test"):
        raise RuntimeError(f"refusing to write invented rows to database {name!r}: a test database name ends in _test")
    if conn.execute(text("SELECT current_user <> session_user")).scalar_one():
        raise RuntimeError("build the rows before act_as: under row-level security the factories' own reads are empty")


def _email() -> str:
    return f"demo.{uuid4().hex[:10]}@example.test"


def _person(conn: Connection, name: str, email: str, *, is_synthetic: bool = True) -> UUID:
    user = make_user(conn, email=email)
    conn.execute(
        text("UPDATE users SET display_name = :name, is_synthetic = :synthetic WHERE id = :u"),
        {"name": name, "synthetic": is_synthetic, "u": user},
    )
    return user


# ---- organizations and members ----------------------------------------


def make_org(conn: Connection, name: str = "Halden Demo", *, timezone: str = "UTC", is_synthetic: bool = True) -> Org:
    """An organization and its owner, an active member. A synthetic organization's name must end in "Demo"."""
    _require_test_database(conn)
    if is_synthetic and not name.endswith("Demo"):
        raise ValueError("an invented organization's name ends in 'Demo'; pass is_synthetic=False for a real one")
    owner = _person(conn, "Demo Owner", _email())
    org_id = conn.execute(
        text(
            """INSERT INTO organizations (name, timezone, is_synthetic, created_by)
               VALUES (:name, :tz, :synthetic, :owner) RETURNING id"""
        ),
        {"name": name, "tz": timezone, "synthetic": is_synthetic, "owner": owner},
    ).scalar_one()
    conn.execute(
        text(
            """INSERT INTO org_members (organization_id, user_id, role, status, joined_at, created_at)
               VALUES (:o, :u, 'owner', 'active', clock_timestamp(), clock_timestamp())"""
        ),
        {"o": org_id, "u": owner},
    )
    return Org(org_id, name, owner)


def make_member(
    conn: Connection, org: Org, *, role: str = "member", invited: bool = False, email: str | None = None
) -> Member:
    """An active member with a new account, or with invited=True a pending invitation: an address and no account (the
    test makes the invited person, with the same address, when it needs one)."""
    _require_test_database(conn)
    email = email or _email()
    if invited:
        row_id = conn.execute(
            text(
                """INSERT INTO org_members (organization_id, invited_email, role, invited_by, created_at)
                   VALUES (:o, :email, CAST(:role AS org_role), :by, clock_timestamp()) RETURNING id"""
            ),
            {"o": org.id, "email": email, "role": role, "by": org.owner},
        ).scalar_one()
        return Member(row_id, org.id, None, email, role, "invited")
    user = _person(conn, f"Demo {role.capitalize()}", email)
    row_id = conn.execute(
        text(
            """INSERT INTO org_members (organization_id, user_id, role, status, joined_at, created_at)
               VALUES (:o, :u, CAST(:role AS org_role), 'active', clock_timestamp(), clock_timestamp()) RETURNING id"""
        ),
        {"o": org.id, "u": user, "role": role},
    ).scalar_one()
    return Member(row_id, org.id, user, email, role, "active")


# ---- roles ----------------------------------------


def _spans(description: str, requirements: Sequence[tuple[str, str]]) -> list[tuple[str, str, int, int]]:
    """(text, importance, start, end) of each requirement, found in the description. Offsets are code points, which is
    what Python indexes and PostgreSQL substr both count."""
    found = []
    for quote, importance in requirements:
        start = description.find(quote)
        if start < 0:
            raise ValueError(f"requirement text is not in the description: {quote!r}")
        found.append((quote, importance, start, start + len(quote)))
    return found


def _insert_role(
    conn: Connection,
    role_id: UUID,
    org: Org,
    *,
    title: str,
    description: str,
    status: str,
    pay: tuple[int, int, str] | None,
    is_confirmed: bool,
    job_id: int | None,
    place: dict[str, Any],
) -> None:
    salary_min, salary_max, currency = pay or (None, None, None)
    conn.execute(
        text(
            """INSERT INTO roles (id, organization_id, title, description, status, country, city, work_arrangements,
                                  employment_type, seniority, salary_min, salary_max, salary_currency,
                                  requirements_confirmed_at, job_id, created_by, opened_at)
               VALUES (:id, :org, :title, :description, CAST(:status AS role_status), :country, :city,
                       CAST(:arrangements AS work_arrangement[]), CAST(:employment_type AS employment_type),
                       CAST(:seniority AS seniority), :pay_min, :pay_max, :currency,
                       CASE WHEN :confirmed THEN now() END, :job, :creator, CASE WHEN :opened THEN now() END)"""
        ),
        {
            "id": role_id,
            "org": org.id,
            "title": title,
            "description": description,
            "status": status,
            "pay_min": salary_min,
            "pay_max": salary_max,
            "currency": currency,
            "confirmed": is_confirmed,
            "job": job_id,
            "creator": org.owner,
            "opened": status == "open",
            **place,
        },
    )


def _insert_requirements(
    conn: Connection, role_id: UUID, org: Org, spans: list[tuple[str, str, int, int]], *, is_confirmed: bool
) -> tuple[UUID, ...]:
    return tuple(
        conn.execute(
            text(
                """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text, span,
                                                  is_confirmed)
                   VALUES (:role, :org, :n, CAST(:importance AS requirement_importance), 'skill', :text,
                           int4range(:start, :end), :confirmed) RETURNING id"""
            ),
            {
                "role": role_id,
                "org": org.id,
                "n": ordinal,
                "importance": importance,
                "text": quote,
                "start": start,
                "end": end,
                "confirmed": is_confirmed,
            },
        ).scalar_one()
        for ordinal, (quote, importance, start, end) in enumerate(spans, start=1)
    )


def _place(
    country: str | None, city: str | None, arrangements: Sequence[str], employment_type: str, seniority: str
) -> dict[str, Any]:
    """The fields a role and its job share: where the work is and what kind of job it is."""
    return {
        "country": country,
        "city": city,
        "arrangements": list(arrangements),
        "employment_type": employment_type,
        "seniority": seniority,
    }


def make_role(
    conn: Connection,
    org: Org,
    *,
    title: str = "Data engineer",
    description: str = DESCRIPTION,
    requirements: Sequence[tuple[str, str]] = REQUIREMENTS,
    pay: tuple[int, int, str] | None = None,
    confirmed: bool = False,
    country: str | None = None,
    city: str | None = None,
    arrangements: Sequence[str] = (),
    employment_type: str = "unknown",
    seniority: str = "unknown",
) -> Role:
    """A draft role created by the organization's owner. `requirements` is (text, importance) in order, each text a
    quote of the description. With confirmed=True every item is confirmed and so is the list: a pay range is then all it
    needs to be published (the publish route of 06-03 makes the job itself)."""
    _require_test_database(conn)
    spans = _spans(description, requirements)
    role_id = uuid4()
    place = _place(country, city, arrangements, employment_type, seniority)
    _insert_role(
        conn,
        role_id,
        org,
        title=title,
        description=description,
        status="draft",
        pay=pay,
        is_confirmed=confirmed,
        job_id=None,
        place=place,
    )
    ids = _insert_requirements(conn, role_id, org, spans, is_confirmed=confirmed)
    return Role(role_id, org.id, title, description, org.name, ids)


def make_open_role(
    conn: Connection,
    org: Org,
    *,
    title: str = "Data engineer",
    description: str = DESCRIPTION,
    requirements: Sequence[tuple[str, str]] = REQUIREMENTS,
    pay: tuple[int, int, str] = PAY,
    country: str | None = None,
    city: str | None = None,
    arrangements: Sequence[str] = (),
    employment_type: str = "unknown",
    seniority: str = "unknown",
) -> Role:
    """An open role, as the publish route will leave it: confirmed requirements, a pay range, and a job in the index
    that is unlisted and belongs to the organization's `direct` source, which the index fetch never touches
    (is_active false). The job's requirements are the role's own, with the same spans (INDEX, ingest.api contract)."""
    _require_test_database(conn)
    spans = _spans(description, requirements)
    role_id = uuid4()
    place = _place(country, city, arrangements, employment_type, seniority)
    source_id = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
               VALUES ('direct', :token, :company, :org, false)
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name RETURNING id"""
        ),
        {"token": str(org.id), "company": org.name, "org": org.id},
    ).scalar_one()
    salary_min, salary_max, currency = pay
    job_id = conn.execute(
        text(
            """INSERT INTO jobs (source_id, external_id, title, company_name, country, city, work_arrangements,
                                 employment_type, seniority, salary_min, salary_max, salary_currency, description,
                                 apply_url, content_hash, dedupe_key, requirements_version, is_listed)
               VALUES (:source, :external, :title, :company, :country, :city, CAST(:arrangements AS work_arrangement[]),
                       CAST(:employment_type AS employment_type), CAST(:seniority AS seniority), :pay_min, :pay_max,
                       :currency, :description, :apply_url, :hash, :dedupe, 1, false)
               RETURNING id"""
        ),
        {
            "source": source_id,
            "external": str(role_id),
            "title": title,
            "company": org.name,
            "pay_min": salary_min,
            "pay_max": salary_max,
            "currency": currency,
            "description": description,
            # Candidates reach a role only through its introduction, so the apply link is the app (INDEX contract).
            "apply_url": f"{config.get().app_base_url}/app/today",
            "hash": hashlib.sha256(description.encode()).hexdigest(),
            "dedupe": f"direct:{role_id}",
            **place,
        },
    ).scalar_one()
    job_requirement_ids = tuple(
        conn.execute(
            text(
                """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                 prompt_version)
                   VALUES (:job, 1, :n, CAST(:importance AS requirement_importance), 'skill', :text,
                           int4range(:start, :end), 'employer-confirmed') RETURNING id"""
            ),
            {"job": job_id, "n": ordinal, "importance": importance, "text": quote, "start": start, "end": end},
        ).scalar_one()
        for ordinal, (quote, importance, start, end) in enumerate(spans, start=1)
    )
    _insert_role(
        conn,
        role_id,
        org,
        title=title,
        description=description,
        status="open",
        pay=pay,
        is_confirmed=True,
        job_id=job_id,
        place=place,
    )
    ids = _insert_requirements(conn, role_id, org, spans, is_confirmed=True)
    return Role(role_id, org.id, title, description, org.name, ids, job_id, job_requirement_ids)


# ---- candidates, decisions, asks ----------------------------------------


def make_candidate(
    conn: Connection,
    *,
    name: str = "Demo Candidate",
    email: str | None = None,
    open_to_introductions: bool = True,
    is_synthetic: bool = True,
) -> Candidate:
    """An invented candidate with a confirmed Career Record (tests/match/factories.py: a resume, three confirmed facts,
    a location constraint of India and a remote or hybrid one) who agreed to AI processing and, unless told otherwise,
    to being asked about roles. Pass is_synthetic=False and an address of another domain for the one test that needs a
    person the relay would deliver to."""
    _require_test_database(conn)
    email = email or _email()
    user = _person(conn, name, email, is_synthetic=is_synthetic)
    record = make_record(conn, user)
    trust.grant_consent(conn, user, "ai_processing", trust.ai_processing_text_version())
    if open_to_introductions:
        trust.grant_consent(conn, user, "open_to_introductions", CONSENT_TEXT)
    return Candidate(user, email, name, record)


def _role_run(conn: Connection, role: Role) -> UUID:
    """A finished role run: trigger role, the role's id, no person. Started and finished by the clock, so a later run
    is later even inside one transaction (role_preview reads the latest)."""
    return conn.execute(
        text(
            """INSERT INTO match_runs (trigger, role_id, local_date, status, ranker_version, evidence_prompt_version,
                                       started_at, finished_at)
               VALUES ('role', :role, (now() AT TIME ZONE 'UTC')::date, 'completed', :ranker, :prompt,
                       clock_timestamp(), clock_timestamp())
               RETURNING id"""
        ),
        {"role": role.id, "ranker": RANKER_VERSION, "prompt": EVIDENCE_PROMPT_VERSION},
    ).scalar_one()


def _default_verdicts(conn: Connection, role: Role, candidate: Candidate) -> list[Verdict]:
    """Every required item met, by the achievement that names the tools; every preferred one unsupported."""
    importances = (
        conn.execute(
            text("SELECT importance::text FROM job_requirements WHERE job_id = :job ORDER BY ordinal"),
            {"job": role.job_id},
        )
        .scalars()
        .all()
    )
    achievement = candidate.record.fact_ids[1]
    return [Verdict("met", (achievement,)) if i == "required" else Verdict("no_evidence") for i in importances]


def make_introduced_decision(
    conn: Connection,
    role: Role,
    candidate: Candidate,
    *,
    run: UUID | None = None,
    outcome: str = "introduced",
    verdicts: Sequence[Verdict] | None = None,
) -> Decision:
    """What a role run records for a candidate: with outcome introduced, a decision with one evidence row per
    requirement of the role's job (every required item met unless `verdicts` says otherwise); with outcome withheld, a
    decision that says a required item is not met, and no evidence. A new completed run is made unless `run` names one,
    so passing the first decision's run puts several candidates in one run, as role_preview counts them."""
    _require_test_database(conn)
    if role.job_id is None:
        raise ValueError("only an open role has a job to introduce a candidate to")
    if outcome not in ("introduced", "withheld"):
        raise ValueError("a role run records a candidate as introduced or withheld")
    introduced = outcome == "introduced"
    if verdicts is not None and not introduced:
        raise ValueError("a withheld decision has no verdicts")
    if introduced:
        verdicts = verdicts if verdicts is not None else _default_verdicts(conn, role, candidate)
        assert len(verdicts) == len(role.job_requirement_ids), "one verdict per requirement"
    run = run or _role_run(conn, role)
    job = Job(role.job_id, role.title, role.company, 1, role.job_requirement_ids)
    decision = make_decision(
        conn,
        run,
        candidate.id,
        job,
        outcome=outcome,
        reasons=("selected_for_role",) if introduced else ("required_not_met",),
        summary="Meets every required item." if introduced else None,
    )
    if verdicts is not None:  # an introduced decision: one evidence row per requirement, as the citation trigger checks
        for requirement_id, verdict in zip(role.job_requirement_ids, verdicts, strict=True):
            conn.execute(
                text(
                    """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, fact_ids,
                                                   note)
                       VALUES (:decision, :u, :requirement, :verdict, CAST(:facts AS uuid[]), :note)"""
                ),
                {
                    "decision": decision,
                    "u": candidate.id,
                    "requirement": requirement_id,
                    "verdict": verdict.verdict,
                    "facts": list(verdict.fact_ids),
                    "note": verdict.note,
                },
            )
    # The completeness check waits for commit, which a rolled-back test never reaches: run it now, then put it back.
    conn.execute(text("SET CONSTRAINTS match_decisions_complete IMMEDIATE"))
    conn.execute(text("SET CONSTRAINTS match_decisions_complete DEFERRED"))
    return Decision(decision, run, role.id, candidate.id, outcome)


def _preview(conn: Connection, candidate: Candidate, decision: UUID) -> dict[str, Any]:
    """What this candidate would share on a yes, in the shape of API.md 3.12 and the INDEX: the name, the headline (the
    statement of the role fact), the evidence table with the record quotes it cites, the confirmed role and education
    facts, and the account email for a yes. Plan 06-05's hiring.preview.build is the production builder of this."""
    rows = conn.execute(
        text(
            """SELECT r.text AS requirement, r.importance::text AS importance, e.verdict::text AS verdict, e.fact_ids
               FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
               WHERE e.match_decision_id = :d ORDER BY r.ordinal"""
        ),
        {"d": decision},
    ).all()
    quote_of = dict(
        conn.execute(
            text(
                """SELECT id, coalesce(source_quote, statement) FROM profile_facts
                   WHERE user_id = :u AND confirmed_at IS NOT NULL AND rejected_at IS NULL AND superseded_at IS NULL"""
            ),
            {"u": candidate.id},
        ).all()
    )
    evidence = [
        {
            "requirement": row.requirement,
            "importance": row.importance,
            "verdict": row.verdict,
            "record_quotes": [quote_of[fact] for fact in row.fact_ids],
        }
        for row in rows
    ]
    facts = [
        {"kind": kind, "statement": statement}
        for kind, statement in conn.execute(
            text(
                """SELECT kind::text, statement FROM profile_facts
                   WHERE user_id = :u AND kind IN ('role', 'education') AND confirmed_at IS NOT NULL
                     AND rejected_at IS NULL AND superseded_at IS NULL ORDER BY created_at, id"""
            ),
            {"u": candidate.id},
        )
    ]
    headline = next((fact["statement"] for fact in facts if fact["kind"] == "role"), None)
    return {
        "on_accept": {"name": candidate.name, "headline": headline, "evidence": evidence, "facts": facts},
        "on_employer_yes": {"email": candidate.email},
    }


def _insert_as_worker(conn: Connection, sql: str, params: dict[str, Any]) -> Row[Any]:
    """Run one INSERT as app_worker, which is who creates asks (app_api has no INSERT grant on introductions). In a
    savepoint, so a refusal by a guard leaves the transaction usable and the role as it was; on success the role goes
    back to the bootstrap login."""
    with conn.begin_nested():
        conn.execute(text("SET LOCAL ROLE app_worker"))
        row = conn.execute(text(sql), params).one()
    conn.execute(text("RESET ROLE"))
    return row


def make_ask(
    conn: Connection,
    role: Role,
    candidate: Candidate,
    *,
    decision: Decision | None = None,
    asked_at: datetime | None = None,
) -> Ask:
    """An introduction in status asked, as the ask job leaves one: an introduced decision for this candidate and the
    role's job (made here unless `decision` is given), a share_preview of what the candidate would share, and an expiry
    seven days after asked_at (the guard sets it). Pass an asked_at in the past for an ask that has already expired."""
    _require_test_database(conn)
    decision = decision or make_introduced_decision(conn, role, candidate)
    preview = _preview(conn, candidate, decision.id)
    row = _insert_as_worker(
        conn,
        """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview, asked_at,
                                      expires_at)
           VALUES (:org, :role, :u, :decision, CAST(:preview AS jsonb), COALESCE(CAST(:asked AS timestamptz), now()),
                   COALESCE(CAST(:asked AS timestamptz), now()) + interval '7 days')
           RETURNING id, asked_at, expires_at""",
        {
            "org": role.org,
            "role": role.id,
            "u": candidate.id,
            "decision": decision.id,
            "preview": json.dumps(preview),
            "asked": asked_at,
        },
    )
    return Ask(row.id, role.org, role.id, candidate.id, decision.id, row.asked_at, row.expires_at, preview)


def make_accepted(conn: Connection, role: Role, candidate: Candidate, *, ask: Ask | None = None) -> Accepted:
    """An accepted introduction as 06-04's accept leaves it: the introduction consent through trust.api, the status
    move (the guard sets responded_at and answer_due_at, five business days on in the organization's time zone), and
    the shortlist entry holding exactly the share_preview. The ask is made here unless `ask` is given."""
    _require_test_database(conn)
    if ask is not None and (ask.role, ask.user) != (role.id, candidate.id):
        raise ValueError("the ask is not this role's ask of this candidate")
    ask = ask or make_ask(conn, role, candidate)
    consent = trust.grant_introduction_consent(conn, candidate.id, role.org, role.id, trust.INTRODUCTION_TEXT_VERSION)
    due = conn.execute(
        text("UPDATE introductions SET status = 'accepted' WHERE id = :i RETURNING answer_due_at"), {"i": ask.id}
    ).scalar_one()
    shortlist = conn.execute(
        text(
            """INSERT INTO shortlist_entries (organization_id, role_id, introduction_id, user_id, shared_payload,
                                              contact_released_at, created_at)
               SELECT organization_id, role_id, id, user_id, share_preview, now(), clock_timestamp()
               FROM introductions WHERE id = :i RETURNING id"""
        ),
        {"i": ask.id},
    ).scalar_one()
    return Accepted(ask, consent, shortlist, due)
