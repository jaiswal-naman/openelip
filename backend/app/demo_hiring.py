"""The demo of the hiring desk (plan 06-07): an invented organization, seven invented candidates who say yes to being
asked, one who blocked the organization, and the signed-in demo accounts made ready, written into a scratch database.

    python -m app.cli demo seed-hiring --employer-email <A> --candidate-email <B>

A and B are accounts that signed in once (so their sign-in codes reach Mailpit); the seed keeps them ordinary
accounts, not synthetic ones. A becomes the owner of "Halden Demo". B gets the same invented record as the six
"Demo Candidate n" accounts and is open to introductions. A seventh invented account has blocked "Halden Demo" (an
excluded_company constraint), so the role run filters it out and it is never asked. Everything is invented and labelled
(backend/app/demo_data/hiring/README.md); the seed does not post the role, ask or score anyone: the browser test
does that through the pages, with the recorded model answers.

It refuses to run unless every database it touches has a name ending in _test and the model provider is the fake one. It
opens connections only with the logins that refusal checks: "migrate" here, never the API login, which does not exist in
the worker container where the seed runs and would fall back to a localhost URL (on a laptop, a real database).
Running it again changes nothing.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app import demo
from app.demo import SeedRefused
from app.platform import db
from app.platform.sessions import seal_bytes
from app.trust import api as trust

DATA = Path(__file__).parent / "demo_data" / "hiring"
ORGANIZATION = "Halden Demo"
TIMEZONE = "Europe/Berlin"
CANDIDATES = 6  # the synthetic candidates who are asked; with B, seven pass the hard filter
BLOCKED_NAME = "Demo Blocked Candidate"
DEFAULT_NAME = "Demo Candidate"  # B keeps a name of their own if they set one
# The wording versions the consents are given under: the one the settings switch sends (web/src/features/introductions)
# and the terms placeholder of the Phase 4 demo.
OPEN_TO_INTRODUCTIONS_VERSION = "open-to-introductions-2026-11"
RESUME_FILENAME = "demo-hiring-resume-invented.docx"
SEED_LOGINS = ("migrate",)  # the roles this module opens a connection with; tests/test_demo_hiring_seed.py checks it


@dataclass(frozen=True)
class HiringSeeded:
    organization_id: UUID
    employer_id: UUID
    candidate_id: UUID
    synthetic_candidates: int
    created: bool  # False when the organization was already there (a second run)

    def report(self) -> str:
        return "\n".join(
            [
                f"{'Seeded' if self.created else 'Already seeded'} the invented hiring demo.",
                f"Organization {ORGANIZATION} ({self.organization_id}): the employer account owns it.",
                f"{self.synthetic_candidates} invented candidates, one of them blocking {ORGANIZATION}; the candidate "
                "account has the same invented record and is open to introductions.",
                "Next: post the role in backend/app/demo_data/hiring/role.txt as the employer (the browser test does).",
            ]
        )


def _record() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DATA / "record.json").read_text(encoding="utf-8"))
    return data


def _organization(conn: Connection, employer: UUID) -> tuple[UUID, bool]:
    found = conn.execute(
        text("SELECT id FROM organizations WHERE name = :name AND is_synthetic"), {"name": ORGANIZATION}
    ).scalar_one_or_none()
    created = found is None
    org_id: UUID = (
        found
        if found is not None
        else conn.execute(
            text(
                """INSERT INTO organizations (name, timezone, is_synthetic, created_by)
                   VALUES (:name, :tz, true, :owner) RETURNING id"""
            ),
            {"name": ORGANIZATION, "tz": TIMEZONE, "owner": employer},
        ).scalar_one()
    )
    if not conn.execute(
        text("SELECT 1 FROM org_members WHERE organization_id = :o AND user_id = :u"), {"o": org_id, "u": employer}
    ).first():
        conn.execute(
            text(
                """INSERT INTO org_members (organization_id, user_id, role, status, joined_at)
                   VALUES (:o, :u, 'owner', 'active', now())"""
            ),
            {"o": org_id, "u": employer},
        )
    return org_id, created


def _synthetic_candidate(conn: Connection, email: str, name: str) -> UUID:
    found = conn.execute(text("SELECT id FROM users WHERE lower(email) = :e"), {"e": email}).scalar_one_or_none()
    if found is not None:
        user_id: UUID = found
        return user_id
    user_id = conn.execute(
        text(
            """INSERT INTO users (email, display_name, is_synthetic, timezone)
               VALUES (:email, :name, true, :tz) RETURNING id"""
        ),
        {"email": email, "name": name, "tz": TIMEZONE},
    ).scalar_one()
    return user_id


def _seed_record(conn: Connection, user_id: UUID) -> None:
    """The invented resume and its confirmed facts, and the record marked confirmed; nothing if it is already there."""
    if conn.execute(
        text("SELECT 1 FROM documents WHERE user_id = :u AND original_filename = :name"),
        {"u": user_id, "name": RESUME_FILENAME},
    ).first():
        return
    record = _record()
    resume = "\n".join(record["resume"])
    file_bytes = demo._docx(resume)
    document_id = conn.execute(
        text(
            """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                      extracted_text, parsed_at)
               VALUES (:u, :content, :name, :mime, :size, :sha, 'parsed', :body, now()) RETURNING id"""
        ),
        {
            "u": user_id,
            "content": seal_bytes(file_bytes),
            "name": RESUME_FILENAME,
            "mime": demo.DOCX_MIME,
            "size": len(file_bytes),
            "sha": hashlib.sha256(file_bytes).digest(),
            "body": resume,
        },
    ).scalar_one()
    for fact in record["facts"]:
        statement = fact["statement"]
        start = resume.find(statement)  # code points, the unit a span is in
        if start < 0:
            raise ValueError(f"a demo fact must be a line of the resume: {statement!r}")
        span = {"start": start, "end": start + len(statement)}
        fact_id = uuid4()
        conn.execute(
            text(
                """INSERT INTO profile_facts (id, user_id, kind, statement, attributes, source, document_id,
                                              source_span, source_quote, confirmed_at)
                   VALUES (:id, :u, CAST(:kind AS fact_kind), :statement, CAST(:attributes AS jsonb), 'resume', :doc,
                           int4range(:start, :stop), :statement, now())"""
            ),
            {
                "id": fact_id,
                "u": user_id,
                "kind": fact["kind"],
                "statement": statement,
                "attributes": json.dumps(fact["attributes"]),
                "doc": document_id,
                "start": span["start"],
                "stop": span["end"],
            },
        )
        confirmed = {
            "id": str(fact_id),
            "kind": fact["kind"],
            "status": "confirmed",
            "statement": statement,
            "attributes": fact["attributes"],
            "source": "resume",
            "parent_fact_id": None,
            "provenance": {"document_id": str(document_id), "span": span, "quote": statement},
        }
        demo._apply(conn, user_id, "fact", "confirm", fact_id, {**confirmed, "status": "unconfirmed"}, confirmed)
    conn.execute(
        text("UPDATE users SET record_confirmed_at = COALESCE(record_confirmed_at, now()) WHERE id = :u"),
        {"u": user_id},
    )


def _block_employer(conn: Connection, user_id: UUID) -> None:
    """An excluded_company constraint naming the organization, written as the candidate's own edit."""
    if conn.execute(
        text("SELECT 1 FROM constraints WHERE user_id = :u AND kind = 'excluded_company'"), {"u": user_id}
    ).first():
        return
    constraint_id = uuid4()
    statement = f"Never introduce me to {ORGANIZATION}."
    snapshot = {
        "id": str(constraint_id),
        "kind": "excluded_company",
        "statement": statement,
        "source": "candidate_edit",
        "company_names": [ORGANIZATION],
    }
    event = demo._apply(conn, user_id, "constraint", "create", constraint_id, None, snapshot)
    conn.execute(
        text(
            """INSERT INTO constraints (id, user_id, kind, statement, company_names, source, profile_event_id)
               VALUES (:id, :u, 'excluded_company', :statement, CAST(:names AS text[]), 'candidate_edit', :event)"""
        ),
        {"id": constraint_id, "u": user_id, "statement": statement, "names": [ORGANIZATION], "event": event},
    )


def _ready_for_asks(conn: Connection, user_id: UUID) -> None:
    """The consents a candidate needs to be asked: AI processing (the evidence is scored) and open to introductions."""
    trust.grant_consent(conn, user_id, "ai_processing", trust.ai_processing_text_version())
    trust.grant_consent(conn, user_id, "open_to_introductions", OPEN_TO_INTRODUCTIONS_VERSION)


def seed_hiring(employer_email: str, candidate_email: str) -> HiringSeeded:
    """Make the employer account the owner of "Halden Demo", the candidate account and six invented ones the people it
    can ask, and one more invented account a person who blocked it. Raises SeedRefused, before writing anything, when
    this is not a scratch database with the fake provider or when either address has no account."""
    demo._refuse_unless_scratch_and_fake()
    if employer_email.strip().lower() == candidate_email.strip().lower():
        raise SeedRefused("the employer and the candidate must be two different accounts.")
    with db.transaction(role="migrate") as conn:
        employer = demo._person(conn, employer_email)
        candidate = demo._person(conn, candidate_email)
        org_id, created = _organization(conn, employer)
        conn.execute(
            text("UPDATE users SET display_name = COALESCE(display_name, :name) WHERE id = :u"),
            {"name": DEFAULT_NAME, "u": candidate},
        )
        people = [candidate]
        for number in range(1, CANDIDATES + 1):
            people.append(
                _synthetic_candidate(conn, f"demo.candidate{number}@example.test", f"Demo Candidate {number}")
            )
        blocked = _synthetic_candidate(conn, "demo.blocked@example.test", BLOCKED_NAME)
        for user_id in [*people, blocked]:
            _seed_record(conn, user_id)
            _ready_for_asks(conn, user_id)
        _block_employer(conn, blocked)
    return HiringSeeded(org_id, employer, candidate, CANDIDATES + 1, created)
