"""The demo of the matches and the morning brief (plan 04-06): an invented candidate, four invented postings and a
recorded model, written into a scratch database so the pages and the email can be tried end to end.

    python -m app.cli demo seed-matches --email <address of an account that has signed in>

Everything it writes is invented and labelled so (backend/app/demo_data/README.md). It refuses to run unless every
database it touches has a name ending in _test and the model provider is the fake one that replays recorded answers, so
it can never put an invented posting into the real index or spend a real model call. The account is the one the person
signed in with: it is given the display name "Demo Candidate" and is NOT marked synthetic, because the relay never
delivers mail to a synthetic account (ARCHITECTURE 5.5) and the demo reads the brief email in Mailpit.

What it does, in order: make the account a confirmed record (a resume, three facts, two hard constraints, the consents),
add the postings, run the match with the recorded answers, then compose the brief, whose email is queued due now.
Running it again for the same account adds no second brief and no second shown match.
"""

import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from docx import Document
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url

from app.brief import api as brief
from app.match import api as match
from app.platform import config, db
from app.platform.config import Role
from app.platform.llm import fake
from app.platform.sessions import seal_bytes
from app.profile import api as profile
from app.trust import api as trust

DATA = Path(__file__).parent / "demo_data"
RECORDING = DATA / "match.evidence.score.json"
DISPLAY_NAME = "Demo Candidate"
RESUME_FILENAME = "demo-resume-invented.docx"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# The wording versions the consents are given under. The brief email's is the string the web sends with the switch
# (web/src/features/brief/queries.ts); the terms have no screen yet.
TERMS_VERSION = "demo-terms-2026-11"
BRIEF_EMAIL_VERSION = "brief-email-2026-11-a"
# The brief email is released no earlier than 06:30 local and expires at noon (brief/compose.py). A local time of 09:00
# to 09:59 is after the release and well before noon, so the email is due at once and cannot already have expired.
LOCAL_HOUR = 9
WINDOW = (7, 11)  # a zone is kept while its local hour is in here, so running the seed again does not move the date

# What the invented resume says, in the order it says it. Each statement is a line of resume.txt, so a fact's quote is
# the text at its span. The role carries the dates the career length is counted from (3+ years of experience needs
# them).
FACTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "role",
        "Backend engineer at Initech Demo, 2019 to 2024.",
        {"employer": "Initech Demo", "title": "Backend engineer", "start_date": "2019-01", "end_date": "2024-06"},
    ),
    ("skill", "Skills: Quillscript, PostgreSQL.", {}),
    ("achievement", "Led a team of three engineers.", {}),
)
CONSTRAINTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("location", "Only jobs in the United States.", {"country_codes": ["US"]}),
    ("remote_policy", "Remote or hybrid jobs only.", {"arrangements": ["remote", "hybrid"]}),
)


class SeedRefused(Exception):
    """The demo will not run here. The message says why, in words for the person at the keyboard."""


@dataclass(frozen=True)
class Seeded:
    """What the seed left behind: the person, the run, and the ids of the matches and the brief (for the report)."""

    email: str
    user_id: UUID
    timezone: str
    run_id: UUID
    run_shown: int
    shown: tuple[tuple[UUID, str], ...]  # (decision id, title) in rank order
    filtered: tuple[tuple[UUID, str], ...]
    brief_id: UUID
    email_brief_id: UUID | None
    outbox_id: UUID | None

    def report(self) -> str:
        lines = [
            f"Seeded the invented demo for {self.email} (time zone {self.timezone}).",
            f"Match run {self.run_id}: {self.run_shown} shown by this run, {len(self.shown)} shown today, "
            f"{len(self.filtered)} filtered by the constraints.",
            *(f"  shown {n}: {decision} {title}" for n, (decision, title) in enumerate(self.shown, start=1)),
            *(f"  filtered: {decision} {title}" for decision, title in self.filtered),
            f"Brief {self.brief_id}; email queued as outbox row {self.outbox_id}, due now.",
        ]
        return "\n".join(lines)


# ---- the refusals ----------------------------------------------------------------------------------------------------


def _refuse_unless_scratch_and_fake() -> None:
    settings = config.get()
    for role in ("migrate", "worker", "match"):  # the three logins the seed uses
        name = make_url(settings.database_url(role)).database or ""
        if not name.endswith("_test"):
            raise SeedRefused(
                f"database {name!r} (the {role} login) does not end in _test. The demo writes invented rows and only "
                "runs on a scratch database; docker-compose.demo.yml starts one called app_demo_test."
            )
    if settings.llm_provider != "fake":
        raise SeedRefused(
            f"LLM_PROVIDER is {settings.llm_provider!r}. The demo replays recorded answers and must never reach a real "
            "model: set LLM_PROVIDER=fake."
        )


@contextmanager
def _scratch(user_id: UUID | None = None, *, role: Role) -> Iterator[Connection]:
    """A transaction on a login the seed uses, after the server has said which database it is connected to: the URL
    names were checked by `_refuse_unless_scratch_and_fake`; this is the same rule on `current_database()` itself, as
    the eval seeds do (05-FIXES B8), before the caller writes a row."""
    with db.transaction(user_id, role=role) as conn:
        name = conn.execute(text("SELECT current_database()")).scalar_one()
        if not (name or "").endswith("_test"):
            raise SeedRefused(
                f"the {role} login is connected to database {name!r}, which does not end in _test. The demo writes "
                "invented rows and only runs on a scratch database."
            )
        yield conn


def _person(conn: Connection, email: str) -> UUID:
    found = conn.execute(
        text("SELECT id FROM users WHERE lower(email) = :email AND status = 'active'"), {"email": email.strip().lower()}
    ).one_or_none()
    if found is None:
        raise SeedRefused(
            f"there is no active account for {email}. Sign in once with that address so the account exists."
        )
    user_id: UUID = found.id
    return user_id


def _refuse_other_postings(conn: Connection, source_token: str) -> None:
    """The recorded answers fit the four invented postings only. Another open posting could be scored first and take an
    answer that was not written for it, so a database that holds any is refused."""
    others = conn.execute(
        text(
            """SELECT count(*) FROM jobs j JOIN job_sources s ON s.id = j.source_id
                WHERE j.status = 'open' AND s.token <> :token"""
        ),
        {"token": source_token},
    ).scalar_one()
    if others:
        raise SeedRefused(
            f"the index holds {others} open postings that are not part of the demo. The recorded answers fit the four "
            "invented postings only: use an empty scratch database."
        )


# ---- the time zone ---------------------------------------------------------------------------------------------------


def in_window(zone: str, now: datetime) -> bool:
    """Whether it is between 07:00 and 11:00 in `zone` at `now`. A name this machine does not know is not."""
    try:
        local = now.astimezone(ZoneInfo(zone))
    except (KeyError, ValueError, OSError):
        return False
    return WINDOW[0] <= local.hour < WINDOW[1]


def demo_zone(now: datetime) -> str:
    """The whole-hour zone in which it is LOCAL_HOUR o'clock at `now`. Etc/GMT zones are signed the other way round:
    Etc/GMT-5 is five hours ahead of UTC."""
    ahead = (LOCAL_HOUR - now.hour) % 24
    if ahead > 14:  # Etc/GMT-14 is the furthest east; beyond it, go west instead
        ahead -= 24
    return "Etc/GMT" if ahead == 0 else f"Etc/GMT{'-' if ahead > 0 else '+'}{abs(ahead)}"


# ---- what the seed writes --------------------------------------------------------------------------------------------


def _docx(text_of_resume: str) -> bytes:
    """The resume as a real DOCX, one paragraph a line, so the stored file is what its name and type say."""
    document = Document()
    for line in text_of_resume.split("\n"):
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _apply(
    conn: Connection, user_id: UUID, entity: str, operation: str, entity_id: UUID, before: Any, after: Any
) -> UUID:
    """One change-log row (its id is returned) and one bump of the record version, as the profile module writes them for
    the candidate's own edits. The record is not marked confirmed here: the seed does that last, after the first run, so
    the worker's once-a-minute onboarding sweep cannot start a run of its own while the seed is still writing."""
    version = conn.execute(
        text("UPDATE users SET record_version = record_version + 1 WHERE id = :u RETURNING record_version"),
        {"u": user_id},
    ).scalar_one()
    event_id: UUID = conn.execute(
        text(
            """INSERT INTO profile_events (user_id, event_type, entity, operation, entity_id, before, after, source,
                                           record_version_after, created_at)
               VALUES (:u, 'applied', CAST(:entity AS profile_entity), CAST(:operation AS profile_operation),
                       :entity_id, CAST(:before AS jsonb), CAST(:after AS jsonb), 'candidate_edit', :version,
                       clock_timestamp())
               RETURNING id"""
        ),
        {
            "u": user_id,
            "entity": entity,
            "operation": operation,
            "entity_id": entity_id,
            "before": _json(before),
            "after": _json(after),
            "version": version,
        },
    ).scalar_one()
    return event_id


def _seed_record(conn: Connection, user_id: UUID) -> None:
    """The resume, its three confirmed facts and the two hard constraints; nothing when the account already has them."""
    if conn.execute(
        text("SELECT 1 FROM documents WHERE user_id = :u AND original_filename = :name"),
        {"u": user_id, "name": RESUME_FILENAME},
    ).first():
        return
    resume = (DATA / "resume.txt").read_text(encoding="utf-8").rstrip("\n")
    file_bytes = _docx(resume)
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
            "mime": DOCX_MIME,
            "size": len(file_bytes),
            "sha": hashlib.sha256(file_bytes).digest(),
            "body": resume,
        },
    ).scalar_one()
    for kind, statement, attributes in FACTS:
        start = resume.find(statement)  # Python counts code points, the unit a span is in
        if start < 0:
            raise ValueError(f"a demo fact must be a line of resume.txt: {statement!r}")
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
                "kind": kind,
                "statement": statement,
                "attributes": json.dumps(attributes),
                "doc": document_id,
                "start": span["start"],
                "stop": span["end"],
            },
        )
        confirmed = {
            "id": str(fact_id),
            "kind": kind,
            "status": "confirmed",
            "statement": statement,
            "attributes": attributes,
            "source": "resume",
            "parent_fact_id": None,
            "provenance": {"document_id": str(document_id), "span": span, "quote": statement},
        }
        _apply(conn, user_id, "fact", "confirm", fact_id, {**confirmed, "status": "unconfirmed"}, confirmed)
    for kind, statement, fields in CONSTRAINTS:
        constraint_id = uuid4()
        snapshot = {
            "id": str(constraint_id),
            "kind": kind,
            "statement": statement,
            "source": "candidate_edit",
            **fields,
        }
        event_id = _apply(conn, user_id, "constraint", "create", constraint_id, None, snapshot)
        conn.execute(
            text(
                """INSERT INTO constraints (id, user_id, kind, statement, country_codes, arrangements, source,
                                            profile_event_id)
                   VALUES (:id, :u, CAST(:kind AS constraint_kind), :statement, CAST(:countries AS char(2)[]),
                           CAST(:arrangements AS work_arrangement[]), 'candidate_edit', :event)"""
            ),
            {
                "id": constraint_id,
                "u": user_id,
                "kind": kind,
                "statement": statement,
                "countries": fields.get("country_codes"),
                "arrangements": fields.get("arrangements"),
                "event": event_id,
            },
        )


def _seed_postings(conn: Connection, data: dict[str, Any]) -> None:
    """The four invented postings and their requirements. A posting that is already there is left as it is."""
    source = data["source"]
    source_id = conn.execute(
        text(
            """INSERT INTO job_sources (ats, token, company_name, is_active)
               VALUES (CAST(:ats AS ats_type), :token, :company, false)
               ON CONFLICT (ats, token) DO UPDATE SET company_name = EXCLUDED.company_name RETURNING id"""
        ),
        {"ats": source["ats"], "token": source["token"], "company": source["company_name"]},
    ).scalar_one()  # inactive: the worker never fetches a board that does not exist
    for posting in data["postings"]:
        salary = posting["salary"] or {}
        description = posting["description"]
        job_id = conn.execute(
            text(
                """INSERT INTO jobs (source_id, external_id, title, company_name, location_raw, country, city,
                                     work_arrangements, employment_type, salary_min, salary_max, salary_currency,
                                     description, apply_url, content_hash, dedupe_key, requirements_version)
                   VALUES (:source, :external, :title, :company, :location_raw, :country, :city,
                           CAST(:arrangements AS work_arrangement[]), CAST(:employment AS employment_type), :salary_min,
                           :salary_max, :currency, :description, :apply_url, :hash, :external, 1)
                   ON CONFLICT (source_id, external_id) DO UPDATE SET title = jobs.title RETURNING id"""
            ),
            {
                "source": source_id,
                "external": posting["external_id"],
                "title": posting["title"],
                "company": source["company_name"],
                "location_raw": posting["location_raw"],
                "country": posting["country"],
                "city": posting["city"],
                "arrangements": posting["work_arrangements"],
                "employment": posting["employment_type"],
                "salary_min": salary.get("min"),
                "salary_max": salary.get("max"),
                "currency": salary.get("currency"),
                "description": description,
                "apply_url": posting["apply_url"],
                "hash": hashlib.sha256(description.encode()).hexdigest(),
            },
        ).scalar_one()
        for ordinal, requirement in enumerate(posting["requirements"], start=1):
            start = description.find(requirement["text"])
            if start < 0:
                raise ValueError(f"a demo requirement must be in its posting's text: {requirement['text']!r}")
            conn.execute(
                text(
                    """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                                     min_years, prompt_version)
                       VALUES (:job, 1, :ordinal, CAST(:importance AS requirement_importance),
                               CAST(:kind AS requirement_kind), :quote, int4range(:start, :stop), :years, 'demo-seed')
                       ON CONFLICT (job_id, content_version, ordinal) DO NOTHING"""
                ),
                {
                    "job": job_id,
                    "ordinal": ordinal,
                    "importance": requirement["importance"],
                    "kind": requirement["kind"],
                    "quote": requirement["text"],
                    "start": start,
                    "stop": start + len(requirement["text"]),
                    "years": requirement["min_years"],
                },
            )


def _prepare_account(conn: Connection, user_id: UUID, now: datetime) -> str:
    """Name the account, set its brief to daily, give it a time zone in which it is morning now, and record its
    consents. A zone that is already in the morning is kept, so a second run lands on the same local date as the
    first."""
    current = conn.execute(text("SELECT timezone FROM users WHERE id = :u"), {"u": user_id}).scalar_one()
    zone = current if in_window(current, now) else demo_zone(now)
    conn.execute(
        text(
            """UPDATE users SET display_name = :name, timezone = :zone, brief_frequency = 'daily', brief_weekday = NULL
                WHERE id = :u"""
        ),
        {"name": DISPLAY_NAME, "zone": zone, "u": user_id},
    )
    trust.grant_consent(conn, user_id, "terms", TERMS_VERSION)
    trust.grant_consent(conn, user_id, "ai_processing", trust.ai_processing_text_version())
    trust.grant_consent(conn, user_id, "brief_email", BRIEF_EMAIL_VERSION)
    return str(zone)


def _todays_decisions(
    conn: Connection, user_id: UUID, day: Any
) -> tuple[list[tuple[UUID, str]], list[tuple[UUID, str]]]:
    """(shown in rank order, filtered by title) of one local date; a job filtered by two runs is listed once."""
    rows = conn.execute(
        text(
            """SELECT DISTINCT ON (d.outcome, d.job_id) d.id, d.outcome::text AS outcome, d.rank_position, j.title
                 FROM match_decisions d JOIN jobs j ON j.id = d.job_id
                WHERE d.user_id = :u AND d.local_date = :day AND d.outcome IN ('shown', 'filtered')
                ORDER BY d.outcome, d.job_id, d.created_at DESC"""
        ),
        {"u": user_id, "day": day},
    ).all()
    shown = sorted((r for r in rows if r.outcome == "shown"), key=lambda r: r.rank_position)
    filtered = sorted((r for r in rows if r.outcome == "filtered"), key=lambda r: r.title)
    return [(r.id, r.title) for r in shown], [(r.id, r.title) for r in filtered]


def seed_matches(email: str) -> Seeded:
    """Make the account a demo candidate, run the match on the invented postings with the recorded answers, and
    compose the brief. Raises SeedRefused, before writing anything, when this is not a scratch database with the fake
    provider, when the index holds postings that are not the demo's, or when no account has this address."""
    _refuse_unless_scratch_and_fake()
    data = json.loads((DATA / "postings.json").read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    with _scratch(role="migrate") as conn:
        user_id = _person(conn, email)
        _refuse_other_postings(conn, data["source"]["token"])
        zone = _prepare_account(conn, user_id, now)
        _seed_postings(conn, data)
        _seed_record(conn, user_id)
    day = now.astimezone(ZoneInfo(zone)).date()

    fake.replay_from(RECORDING)  # the model is the recording: nothing reaches a provider
    result = match.run(user_id, trigger="on_demand", local_date=day)
    with _scratch(role="migrate") as conn:  # last, so that the onboarding sweep did not see a confirmed record early
        conn.execute(
            text("UPDATE users SET record_confirmed_at = COALESCE(record_confirmed_at, now()) WHERE id = :u"),
            {"u": user_id},
        )
    with _scratch(role="worker") as conn:
        composed = brief.compose(conn, user_id, day, now=now)
        shown, filtered = _todays_decisions(conn, user_id, day)
    return Seeded(
        email=email,
        user_id=user_id,
        timezone=zone,
        run_id=result.run_id,
        run_shown=result.shown,
        shown=tuple(shown),
        filtered=tuple(filtered),
        brief_id=composed.in_app_brief_id,
        email_brief_id=composed.email_brief_id,
        outbox_id=composed.outbox_id,
    )


# ---- the tailoring demo (plan 05-07) ---------------------------------------------------------------------------------

PHASE5 = DATA / "phase5"
RESUME5_FILENAME = "demo-resume-2-invented.docx"


@dataclass(frozen=True)
class SeededTailor:
    """What `seed_tailor` left behind: the person and the facts it added (kind, statement, id), in the order the demo
    recordings number them (the Phase 4 record holds f1 to f3, so these are f4 onward)."""

    email: str
    user_id: UUID
    facts: tuple[tuple[str, str, UUID], ...]
    already: bool = False

    def report(self) -> str:
        if self.already:
            return f"The invented tailoring demo is already seeded for {self.email}; nothing was changed."
        return "\n".join(
            [
                f"Seeded the invented tailoring demo for {self.email}.",
                f"{len(self.facts)} confirmed facts added; the record version moved with each.",
                *(
                    f"  f{n}: {kind} {statement} ({fact_id})"
                    for n, (kind, statement, fact_id) in enumerate(self.facts, 4)
                ),
            ]
        )


def _added_facts(conn: Connection, user_id: UUID) -> list[tuple[str, str, UUID]]:
    """The facts this seed added in the usable order (the recordings' alias order), minus the Phase 4 three."""
    rows = conn.execute(
        text(
            """SELECT kind::text AS kind, statement, id FROM profile_facts
                WHERE user_id = :u AND confirmed_at IS NOT NULL AND rejected_at IS NULL AND superseded_at IS NULL
                ORDER BY created_at, COALESCE(lower(source_span), 2147483647), id"""
        ),
        {"u": user_id},
    ).all()
    return [(r.kind, r.statement, r.id) for r in rows[len(FACTS) :]]


def _seed_resume_two(conn: Connection, user_id: UUID) -> None:
    """The second invented resume, parsed, with its confirmed facts: two roles, their achievements and three skills."""
    resume = (PHASE5 / "resume.txt").read_text(encoding="utf-8").rstrip("\n")
    file_bytes = _docx(resume)
    document_id = conn.execute(
        text(
            """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                      extracted_text, parsed_at)
               VALUES (:u, :content, :name, :mime, :size, :sha, 'parsed', :body, now()) RETURNING id"""
        ),
        {
            "u": user_id,
            "content": seal_bytes(file_bytes),
            "name": RESUME5_FILENAME,
            "mime": DOCX_MIME,
            "size": len(file_bytes),
            "sha": hashlib.sha256(file_bytes).digest(),
            "body": resume,
        },
    ).scalar_one()
    facts = json.loads((PHASE5 / "facts.json").read_text(encoding="utf-8"))["facts"]
    ids = [uuid4() for _ in facts]
    for fact_id, fact in zip(ids, facts, strict=True):
        statement = fact["statement"]
        start = resume.find(statement)  # Python counts code points, the unit a span is in
        if start < 0:
            raise ValueError(f"a demo fact must be a line of resume.txt: {statement!r}")
        parent = ids[fact["parent"]] if "parent" in fact else None
        conn.execute(
            text(
                """INSERT INTO profile_facts (id, user_id, kind, statement, attributes, parent_fact_id, source,
                                              document_id, source_span, source_quote, confirmed_at)
                   VALUES (:id, :u, CAST(:kind AS fact_kind), :statement, CAST(:attributes AS jsonb), :parent,
                           'resume', :doc, int4range(:start, :stop), :statement, now())"""
            ),
            {
                "id": fact_id,
                "u": user_id,
                "kind": fact["kind"],
                "statement": statement,
                "attributes": json.dumps(fact["attributes"]),
                "parent": parent,
                "doc": document_id,
                "start": start,
                "stop": start + len(statement),
            },
        )
        confirmed = {
            "id": str(fact_id),
            "kind": fact["kind"],
            "status": "confirmed",
            "statement": statement,
            "attributes": fact["attributes"],
            "source": "resume",
            "parent_fact_id": None if parent is None else str(parent),
            "provenance": {"document_id": str(document_id), "span": {"start": start, "end": start + len(statement)}},
        }
        _apply(conn, user_id, "fact", "confirm", fact_id, {**confirmed, "status": "unconfirmed"}, confirmed)


def _say_in_chat(user_id: UUID, skill: str) -> None:
    """A skill the candidate stated in chat and confirmed: a proposal from origin `chat`, then its confirmation, through
    the profile module, so the fact is a confirmed_proposal the base resume does not show until an edit adds it. On the
    owner's login, like the rest of the seed: the worker container that runs it holds no API login, and the scratch
    check above covers only the logins the seed uses."""
    with _scratch(user_id, role="migrate") as conn:
        proposal = profile.propose_change(
            conn,
            user_id,
            origin="chat",
            entity="fact",
            operation="create",
            after={"kind": "skill", "statement": skill},
            reason="Invented demo data: said in chat.",
        )
        profile.confirm(conn, user_id, proposal)


def seed_tailor(email: str) -> SeededTailor:
    """Add the invented facts the tailoring demo needs to an account the Phase 4 demo seeded. Raises SeedRefused,
    before writing anything, when this is not a scratch database with the fake provider, when no account has this
    address, or when `seed_matches` has not run for it. A second run changes nothing."""
    _refuse_unless_scratch_and_fake()
    said = json.loads((PHASE5 / "facts.json").read_text(encoding="utf-8"))["said_in_chat"]
    with _scratch(role="migrate") as conn:
        user_id = _person(conn, email)

        def has(name: str) -> bool:
            return (
                conn.execute(
                    text("SELECT 1 FROM documents WHERE user_id = :u AND original_filename = :name"),
                    {"u": user_id, "name": name},
                ).first()
                is not None
            )

        if not has(RESUME_FILENAME):
            raise SeedRefused(f"the matches demo has not been seeded for {email}: run `demo seed-matches` first.")
        resume_two = has(RESUME5_FILENAME)
        chatted = conn.execute(
            text(
                """SELECT count(*) FROM profile_facts WHERE user_id = :u AND source = 'confirmed_proposal'
                      AND statement = ANY(:names)"""
            ),
            {"u": user_id, "names": said},
        ).scalar_one()
        if resume_two and chatted == len(said):
            return SeededTailor(email, user_id, tuple(_added_facts(conn, user_id)), already=True)
        if resume_two:
            raise SeedRefused("the tailoring demo is only partly seeded for this account: use a fresh scratch stack.")
        _seed_resume_two(conn, user_id)
    for skill in said:  # after the resume's facts, so they come last in the record's order
        _say_in_chat(user_id, skill)
    with _scratch(role="migrate") as conn:
        return SeededTailor(email, user_id, tuple(_added_facts(conn, user_id)))


# ---- the campaign demo (plan 07-07) ----------------------------------------------------------------------------------

CAMPAIGN_DATA = DATA / "campaign"


@dataclass(frozen=True)
class SeededCampaign:
    """What `seed_campaign` left behind: the person, the first match's job, and its ready tailored resume."""

    email: str
    user_id: UUID
    job_id: int
    tailored_resume_id: UUID
    already: bool = False

    def report(self) -> str:
        said = "is already seeded" if self.already else "was seeded"
        return "\n".join(
            [
                f"The invented campaign demo {said} for {self.email}; the first match's job id is {self.job_id}.",
                f"A ready tailored resume ({self.tailored_resume_id}) is waiting for it. Open the match and press "
                "Start campaign.",
            ]
        )


def _refuse_unless_pages() -> None:
    """The Researcher reads the invented company site from disk: without that directory it would try the network."""
    if not config.get().research_pages_dir:
        raise SeedRefused(
            "RESEARCH_PAGES_DIR is not set. The campaign demo reads invented company pages from "
            "app/demo_data/campaign/pages and must never fetch a real site: set it to that directory."
        )


def _first_match_job(conn: Connection, user_id: UUID) -> int:
    first = json.loads((DATA / "postings.json").read_text(encoding="utf-8"))["postings"][0]["title"]
    found = conn.execute(
        text(
            """SELECT j.id FROM jobs j WHERE j.title = :title AND EXISTS
                 (SELECT 1 FROM match_decisions d WHERE d.user_id = :u AND d.job_id = j.id AND d.outcome = 'shown')"""
        ),
        {"title": first, "u": user_id},
    ).scalar_one_or_none()
    if found is None:
        raise SeedRefused("the matches demo shows this account no first match: use a fresh scratch database.")
    job_id: int = found
    return job_id


def seed_campaign(email: str) -> SeededCampaign:
    """Get an account ready for the campaign demo: the matches and tailoring demos if they have not run, and a ready
    tailored resume for the first match (written without a model call, so the campaign's packet has one to start from).
    Raises SeedRefused, before writing anything, when this is not a scratch database with the fake provider, when the
    page directory is not set, or when no account has this address. A second run changes nothing."""
    _refuse_unless_scratch_and_fake()
    _refuse_unless_pages()
    with _scratch(role="migrate") as conn:
        user_id = _person(conn, email)
        names = {
            r.original_filename
            for r in conn.execute(text("SELECT original_filename FROM documents WHERE user_id = :u"), {"u": user_id})
        }
    if RESUME_FILENAME not in names:
        seed_matches(email)
    if RESUME5_FILENAME not in names:
        seed_tailor(email)
    with _scratch(role="migrate") as conn:
        job_id = _first_match_job(conn, user_id)
        have = conn.execute(
            text("SELECT id FROM tailored_resumes WHERE user_id = :u AND job_id = :j AND status = 'ready'"),
            {"u": user_id, "j": job_id},
        ).scalar_one_or_none()
        if have is not None:
            return SeededCampaign(email, user_id, job_id, have, already=True)
        made = conn.execute(
            text(
                """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version,
                                                 status, prompt_version, discarded_count)
                   SELECT :u, j.id, j.content_version,
                          (SELECT id FROM documents WHERE user_id = :u AND original_filename = :doc),
                          (SELECT record_version FROM users WHERE id = :u), 'ready', 'demo-seed', 0
                     FROM jobs j WHERE j.id = :j
                   RETURNING id"""
            ),
            {"u": user_id, "j": job_id, "doc": RESUME5_FILENAME},
        ).scalar_one()
        return SeededCampaign(email, user_id, job_id, made)


@dataclass(frozen=True)
class Aged:
    campaigns: int
    steps: int
    wake_jobs: int

    def report(self, days: int) -> str:
        return (
            f"Moved the clock {days} days on: {self.campaigns} campaigns, {self.steps} steps and "
            f"{self.wake_jobs} wake jobs."
        )


def age_campaign(email: str, days: int) -> Aged:
    """Move the account's open campaigns `days` days into the past: the last news, each step's due, start and end times
    and each queued wake job's run time, so a follow-up's quiet days have passed without waiting for them. Same
    refusals as `seed_campaign`; a number of days outside 1 to 365 is refused."""
    _refuse_unless_scratch_and_fake()
    _refuse_unless_pages()
    if not 1 <= days <= 365:
        raise SeedRefused("days must be between 1 and 365.")
    gap = f"{int(days)} days"
    with _scratch(role="migrate") as conn:
        user_id = _person(conn, email)
        ids = [
            str(r.id)
            for r in conn.execute(
                text("SELECT id FROM campaigns WHERE user_id = :u AND stage <> 'closed'"), {"u": user_id}
            )
        ]
        campaigns = conn.execute(
            text(
                """UPDATE campaigns SET last_update_at = last_update_at - CAST(:gap AS interval)
                    WHERE id = ANY(CAST(:ids AS uuid[]))"""
            ),
            {"gap": gap, "ids": ids},
        ).rowcount
        steps = conn.execute(
            text(
                """UPDATE campaign_steps SET due_at = due_at - CAST(:gap AS interval),
                                             started_at = started_at - CAST(:gap AS interval),
                                             finished_at = finished_at - CAST(:gap AS interval)
                    WHERE campaign_id = ANY(CAST(:ids AS uuid[]))"""
            ),
            {"gap": gap, "ids": ids},
        ).rowcount
        wake = conn.execute(
            text(
                """UPDATE queue_jobs SET run_at = run_at - CAST(:gap AS interval)
                    WHERE kind = 'campaign.wake' AND status = 'queued' AND payload ->> 'campaign_id' = ANY(:ids)"""
            ),
            {"gap": gap, "ids": ids},
        ).rowcount
    return Aged(campaigns, steps, wake)
