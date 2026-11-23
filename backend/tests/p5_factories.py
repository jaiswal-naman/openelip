"""Invented rows for the Phase 5 tests (plan 05-01): a record with enough in it to tailor, chat sessions, tailored
resumes with their edits, application packets and applications.

Everything here is made up: the candidate, the employers ("Initech Demo", "Globex Demo"), the school and every address
(example.test). Like the Phase 4 factories, the functions write plain SQL on the connection they are given, which is the
test's own bootstrap connection (it bypasses row-level security; the `conn` fixture rolls its transaction back), and
nothing here commits. They also re-export the Phase 4 factories a Phase 5 test needs, so one import line does:

    from tests.p5_factories import make_user, make_tailorable_record, make_job, make_tailored_resume, make_packet
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from tests.match.factories import (
    LOCAL_DATE,
    Job,
    Record,
    Verdict,
    make_decision,
    make_job,
    make_record,
    make_run,
    make_shown_decision,
    make_user,
)

__all__ = [
    "LOCAL_DATE",
    "TAILOR_RESUME_TEXT",
    "Edit",
    "Job",
    "Record",
    "TailorableRecord",
    "Verdict",
    "make_application",
    "make_chat_session",
    "make_decision",
    "make_job",
    "make_packet",
    "make_record",
    "make_run",
    "make_shown_decision",
    "make_tailorable_record",
    "make_tailored_resume",
    "make_user",
]

TAILOR_PROMPT_VERSION = "test-tailor-v1"

# One invented resume with two roles, a skills line, a school and a certificate. Each fact below is a line, or a word of
# the skills line, so its span and quote are real positions in the text.
ROLE_A = "Senior data engineer, Initech Demo, 2021-03 to 2024-06"
BULLET_A1 = "Built nightly Python and PostgreSQL pipelines that cut report time by 40 percent."
BULLET_A2 = "Led a team of three engineers on the reporting platform."
ROLE_B = "Data analyst, Globex Demo, 2018-01 to 2021-02"
BULLET_B1 = "Wrote SQL reports for the finance team."
BULLET_B2 = "Automated month-end checks with Python scripts."
SKILLS = "Python, PostgreSQL, Quillscript"
EDUCATION = "BSc Computer Science, Example University, 2017"
CERTIFICATION = "Demo Data Practitioner Certificate, 2022"
TAILOR_RESUME_TEXT = "\n".join(
    [
        "Ada Example",
        "ada@example.test",
        "",
        "Experience",
        ROLE_A,
        BULLET_A1,
        BULLET_A2,
        ROLE_B,
        BULLET_B1,
        BULLET_B2,
        "",
        "Skills",
        SKILLS,
        "",
        "Education",
        EDUCATION,
        "",
        "Certifications",
        CERTIFICATION,
        "",
    ]
)


@dataclass(frozen=True)
class TailorableRecord:
    """What make_tailorable_record wrote: the resume document and every confirmed fact by name. The names are
    role_initech, bullet_initech_1, bullet_initech_2, role_globex, bullet_globex_1, bullet_globex_2, skill_python,
    skill_postgresql, skill_quillscript, education and certification."""

    document_id: UUID
    facts: dict[str, UUID]


@dataclass(frozen=True)
class Edit:
    """One resume edit as make_tailored_resume stores it. supporting holds fact ids (from TailorableRecord.facts)."""

    before: str
    after: str
    supporting: tuple[UUID, ...] = ()
    status: str = "passed"  # edit_verifier_status
    reason: str | None = None  # edit_discard_reason, for a discarded edit
    decision: str = "pending"  # edit_decision
    section: str = "experience"
    target_fact_id: UUID | None = None


def make_tailorable_record(conn: Connection, user_id: UUID) -> TailorableRecord:
    """A confirmed Career Record with what tailoring needs: a parsed resume, two roles (employer, title and dates in
    their attributes) with two achievements each, three skills, one education and one certification, all confirmed and
    all quoting the resume, and the record header set (version 1, confirmed)."""
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
            "size": len(TAILOR_RESUME_TEXT),
            "sha": hashlib.sha256(TAILOR_RESUME_TEXT.encode()).digest(),
            "body": TAILOR_RESUME_TEXT,
        },
    ).scalar_one()

    def fact(kind: str, statement: str, attributes: dict, *, at: int, parent: UUID | None = None) -> UUID:
        """A confirmed fact whose quote is the statement, found in the resume from position `at` on."""
        start = TAILOR_RESUME_TEXT.index(statement, at)
        return conn.execute(
            text(
                """INSERT INTO profile_facts (user_id, kind, statement, attributes, parent_fact_id, source, document_id,
                                              source_span, source_quote, confirmed_at)
                   VALUES (:u, :kind, :s, CAST(:attributes AS jsonb), :parent, 'resume', :doc,
                           int4range(:start, :stop), :s, now())
                   RETURNING id"""
            ),
            {
                "u": user_id,
                "kind": kind,
                "s": statement,
                "attributes": json.dumps(attributes),
                "parent": parent,
                "doc": document_id,
                "start": start,
                "stop": start + len(statement),
            },
        ).scalar_one()

    def at(line: str) -> int:
        """Where a line starts, so a word is looked for in its own line and not in an earlier one."""
        return TAILOR_RESUME_TEXT.index(line)

    initech = fact(
        "role",
        ROLE_A,
        {"employer": "Initech Demo", "title": "Senior data engineer", "start_date": "2021-03", "end_date": "2024-06"},
        at=0,
    )
    globex = fact(
        "role",
        ROLE_B,
        {"employer": "Globex Demo", "title": "Data analyst", "start_date": "2018-01", "end_date": "2021-02"},
        at=0,
    )
    facts = {
        "role_initech": initech,
        "bullet_initech_1": fact("achievement", BULLET_A1, {}, at=at(BULLET_A1), parent=initech),
        "bullet_initech_2": fact("achievement", BULLET_A2, {}, at=at(BULLET_A2), parent=initech),
        "role_globex": globex,
        "bullet_globex_1": fact("achievement", BULLET_B1, {}, at=at(BULLET_B1), parent=globex),
        "bullet_globex_2": fact("achievement", BULLET_B2, {}, at=at(BULLET_B2), parent=globex),
        # a skill is its own word of the skills line, as the extractor stores it
        "skill_python": fact("skill", "Python", {"name": "Python"}, at=at(SKILLS)),
        "skill_postgresql": fact("skill", "PostgreSQL", {"name": "PostgreSQL"}, at=at(SKILLS)),
        "skill_quillscript": fact("skill", "Quillscript", {"name": "Quillscript"}, at=at(SKILLS)),
        "education": fact(
            "education",
            EDUCATION,
            {"institution": "Example University", "degree": "BSc Computer Science", "end_date": "2017"},
            at=0,
        ),
        "certification": fact(
            "certification", CERTIFICATION, {"name": "Demo Data Practitioner Certificate", "end_date": "2022"}, at=0
        ),
    }
    conn.execute(text("UPDATE users SET record_version = 1, record_confirmed_at = now() WHERE id = :u"), {"u": user_id})
    return TailorableRecord(document_id, facts)


def make_chat_session(conn: Connection, user_id: UUID, kind: str = "open") -> UUID:
    """An empty chat session of the given kind (`open` or `onboarding`)."""
    return conn.execute(
        text("INSERT INTO chat_sessions (user_id, kind) VALUES (:u, :kind) RETURNING id"), {"u": user_id, "kind": kind}
    ).scalar_one()


def make_tailored_resume(
    conn: Connection, user_id: UUID, job: Job, *, status: str = "ready", edits: Sequence[Edit] = ()
) -> UUID:
    """A tailored resume for the job, on the person's parsed resume and current record version, with the edits as given
    (numbered from 1 in order). A ready one carries a prompt version, and the discarded edits are counted."""
    resume = conn.execute(
        text(
            """INSERT INTO tailored_resumes (user_id, job_id, job_content_version, base_document_id, record_version,
                                             status, prompt_version, discarded_count)
               VALUES (:u, :j, :v,
                       (SELECT id FROM documents WHERE user_id = :u AND status = 'parsed' ORDER BY created_at LIMIT 1),
                       (SELECT record_version FROM users WHERE id = :u), :status,
                       CASE WHEN CAST(:status AS tailored_status) = 'ready' THEN CAST(:prompt AS text) END, :discarded)
               RETURNING id"""
        ),
        {
            "u": user_id,
            "j": job.id,
            "v": job.content_version,
            "status": status,
            "prompt": TAILOR_PROMPT_VERSION,
            "discarded": sum(edit.status == "discarded" for edit in edits),
        },
    ).scalar_one()
    for ordinal, edit in enumerate(edits, start=1):
        conn.execute(
            text(
                """INSERT INTO resume_edits (tailored_resume_id, user_id, ordinal, section, target_fact_id, before_text,
                                             after_text, supporting_fact_ids, verifier_status, discard_reason, decision,
                                             decided_at)
                   VALUES (:r, :u, :ordinal, :section, :target, :before, :after, CAST(:support AS uuid[]), :status,
                           :reason, :decision, CASE WHEN :decided THEN now() END)"""
            ),
            {
                "r": resume,
                "u": user_id,
                "ordinal": ordinal,
                "section": edit.section,
                "target": edit.target_fact_id,
                "before": edit.before,
                "after": edit.after,
                "support": list(edit.supporting),
                "status": edit.status,
                "reason": edit.reason,
                "decision": edit.decision,
                "decided": edit.decision != "pending",
            },
        )
    return resume


def make_packet(conn: Connection, user_id: UUID, job: Job, *, status: str = "ready") -> UUID:
    """An application packet for the job without a campaign: the person's latest tailored resume for it (if any), two
    drafted answers in the shape Phase 5 stores and a short note. A superseded one makes room for the next."""
    answers = [
        {
            "question_id": "why_role",
            "question": "Why this role?",
            "answer": "I build nightly data pipelines and would like to do that for your team.",
            "fact_ids": [],
            "status": "drafted",
            "removed_sentences": 0,
        },
        {
            "question_id": "expected_pay",
            "question": "What pay do you expect?",
            "answer": "",
            "fact_ids": [],
            "status": "left_for_you",
            "removed_sentences": 0,
        },
    ]
    return conn.execute(
        text(
            """INSERT INTO application_packets (user_id, job_id, tailored_resume_id, answers, cover_note, status,
                                                approved_at)
               VALUES (:u, :j,
                       (SELECT id FROM tailored_resumes WHERE user_id = :u AND job_id = :j
                         ORDER BY created_at DESC, id LIMIT 1),
                       CAST(:answers AS jsonb), 'A short invented note for the demo.', :status,
                       CASE WHEN CAST(:status AS packet_status) = 'approved' THEN now() END)
               RETURNING id"""
        ),
        {"u": user_id, "j": job.id, "answers": json.dumps(answers), "status": status},
    ).scalar_one()


def make_application(conn: Connection, user_id: UUID, job: Job, *, status: str = "applied") -> UUID:
    """An application for the job with its history: applied ten days ago and, for any other status, that status two days
    ago. The application row carries the latest status and its time, as the tracker keeps them."""
    changed = "now() - interval '10 days'" if status == "applied" else "now() - interval '2 days'"
    application = conn.execute(
        text(
            f"""INSERT INTO applications (user_id, job_id, status, status_changed_at)
                VALUES (:u, :j, :status, {changed}) RETURNING id"""
        ),
        {"u": user_id, "j": job.id, "status": status},
    ).scalar_one()
    insert = """INSERT INTO application_events (application_id, user_id, status, occurred_at)
                VALUES (:a, :u, :status, {when})"""
    conn.execute(
        text(insert.format(when="now() - interval '10 days'")), {"a": application, "u": user_id, "status": "applied"}
    )
    if status != "applied":
        conn.execute(
            text(insert.format(when="now() - interval '2 days'")), {"a": application, "u": user_id, "status": status}
        )
    return application
