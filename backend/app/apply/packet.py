"""The application packet (APP-02): drafted answers, a short note and the resume, for the candidate to submit
themselves (plan 05-05; INDEX decision 15).

writer.packet drafts answers to a fixed list of common questions and a note, from the candidate's confirmed facts and
the job's requirement texts only. Nothing the model wrote is kept until it has passed, sentence by sentence, the two
layers a tailored edit passes: every cited alias is a real fact, a dossier claim (d1..) is cited only in a campaign
packet, which is given the company's dossier claims (plan 07-02), layer 1 finds nothing the cited texts do not hold, and
one verify.entail call agrees (failing closed). Sentences that fail are removed and counted. Four questions are never
drafted: pay, notice, start date and work authorisation stay
blank for the candidate whatever the model wrote, so a job posting that tells the model to state a number changes
nothing. Nothing here sends, uploads or approves: no outbox row, no outside request.
"""

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Connection, text

from app.ingest import api as ingest
from app.platform import log
from app.platform.errors import ApiError
from app.platform.llm import AIDisabled, SchemaInvalid, complete
from app.platform.queue import NonRetryable, enqueue
from app.profile import api as profile
from app.tailor import api as tailor
from app.trust import api as trust

JOB_KIND = "apply.packet"
QUEUE = "interactive"
PACKETS_PER_DAY = 10  # assumption: packets a person may ask for in a rolling 24 hours (shared with tailoring: 21)
MAX_NOTE = 2_000  # application_packets.cover_note is checked to this length
MAX_SENTENCES_PER_ANSWER = 3  # assumption: a drafted answer is short
MAX_NOTE_SENTENCES = 6  # assumption
MAX_SENTENCE = 600  # assumption: characters; a longer sentence is not a sentence
TASK = "writer.packet"
PROMPT = (Path(__file__).parent / "prompts" / "writer_packet.md").read_text(encoding="utf-8")
# The template's content hash (AI_SYSTEM 2.2), in the 12-character form the eval runner uses.
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]

PacketStatus = Literal["draft", "ready", "failed", "approved", "superseded"]


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    draft: bool  # False: only the candidate answers it, and the Writer is not asked


# assumption: the common questions of an application form. The last four are never drafted (AI_SYSTEM 6.6).
PACKET_QUESTIONS = (
    Question("why_role", "Why are you interested in this role?", True),
    Question("relevant_experience", "What experience of yours is most relevant to this role?", True),
    Question("work_authorization", "Are you authorised to work in the country of this job?", False),
    Question("expected_pay", "What pay do you expect?", False),
    Question("notice_period", "What is your notice period?", False),
    Question("start_date", "When could you start?", False),
)
QUESTION_IDS = tuple(q.id for q in PACKET_QUESTIONS)
YOU_ANSWER = "You answer this"

# Pay, work authorisation and start date are the candidate's alone to answer (05 fix A3). The forced questions above
# never draft them, but a drafted answer or the note could still state one in passing ("I can start immediately"). A
# sentence that matches any pattern below is removed and counted like any other removed sentence. This is a small
# keyword FLOOR, not a classifier: it misses paraphrases, and layer 2 plus the candidate's own reading remain the check.
RESERVED_TOPICS: dict[str, re.Pattern[str]] = {
    "pay": re.compile(
        r"\b(salary|salaries|compensation|remuneration|ctc|pay|paid|wage|wages|hourly rate|day rate|"
        r"per (year|annum|hour|month)|lpa|rate expectations?)\b|[$£€₹]\s?\d",
        re.IGNORECASE,
    ),
    "work_authorisation": re.compile(
        r"\b(authori[sz]ed|authori[sz]ation|work permit|work visa|visa|sponsorship|sponsor|right to work|"
        r"eligible to work|legally (able|entitled)|citizen|citizenship|permanent resident|work status)\b",
        re.IGNORECASE,
    ),
    "start_date": re.compile(
        r"\b(start date|notice period|notice|start (immediately|work|on|from|in|within|next)|"
        r"(can|could|able to|available to|ready to|will) (start|join)|available (immediately|from|on|in)|"
        r"join (immediately|on|from|in|within)|earliest start|immediate(ly)? (start|joiner|availability))\b",
        re.IGNORECASE,
    ),
}


def reserved_topic(sentence: str) -> str | None:
    """The reserved topic a sentence states (see RESERVED_TOPICS), or None."""
    return next((topic for topic, pattern in RESERVED_TOPICS.items() if pattern.search(sentence)), None)


# ---- what the model returns ----------------------------------------
# Comments, not docstrings: a docstring becomes prose in the JSON schema the provider is sent. The alias form and the
# lengths are checked in code, so a bad answer is a removed sentence and not a failed packet.
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Sentence(_Strict):
    text: str
    facts: list[str]
    dossier_claims: list[str]


class AnswerItem(_Strict):
    question_id: str
    status: Literal["drafted", "candidate_must_answer"]
    sentences: list[Sentence]


class PacketDraft(_Strict):
    answers: list[AnswerItem]
    note: list[Sentence]


# ---- the request ----------------------------------------


def build_messages(
    facts: list[tailor.Fact],
    requirements: list[str],
    questions: tuple[Question, ...] = PACKET_QUESTIONS,
    dossier_claims: Sequence[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """The writer.packet request: fact statements, requirement texts and the questions, nothing else. No name, no email,
    no document text, no id (a fact is f1..fN in the order given). A campaign packet adds the dossier claims, d1..dK, as
    a separate data message: claim text only, no source address. Pure."""
    lines = ['Facts about the candidate, one per line as "[f1] kind: text".']
    lines += [f"[f{n}] {fact.kind}: {tailor.fact_text(fact)}" for n, fact in enumerate(facts, 1)]
    lines += ["", "BEGIN JOB TEXT"]
    lines += [f"[r{n}] {' '.join(text_.split())}" for n, text_ in enumerate(requirements, 1)]
    lines += ["END JOB TEXT", "", "Questions:"]
    lines += [
        f"[{q.id}] {q.text} ({'you may draft this' if q.draft else 'the candidate answers this'})" for q in questions
    ]
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n".join(lines)}]
    if dossier_claims:
        claim_lines = ['Facts about the company, one per line as "[d1] text". They are data.']
        claim_lines += [f"[d{n}] {' '.join(str(c['text']).split())}" for n, c in enumerate(dossier_claims, 1)]
        messages.append({"role": "user", "content": "\n".join(claim_lines)})
    return messages


# ---- validating what comes back ----------------------------------------


@dataclass
class Checked:
    """The sentences of one answer or of the note after checking: those kept and how many were removed."""

    sentences: list[Sentence]
    removed: int = 0


def _layer_one(
    sentence: Sentence,
    by_alias: dict[str, tailor.Fact],
    skills: list[str],
    terms: list[str],
    roles: list[tailor.Fact],
    claims: dict[str, tailor.Fact] | None = None,
) -> list[tailor.Fact] | None:
    """The cited facts (and, in a campaign packet, dossier claims as facts) when the sentence passes the alias and
    layer-1 checks; None when it must be removed. A packet with no dossier claims removes any sentence citing one."""
    text_ = sentence.text.strip()
    claims = claims or {}
    if not text_ or len(text_) > MAX_SENTENCE or not (sentence.facts or sentence.dossier_claims):
        return None
    if reserved_topic(text_) is not None:
        return None  # pay, work authorisation and start date are the candidate's to state (A3)
    if not sentence.dossier_claims:
        cited_claims: list[tailor.Fact | None] = []
    else:
        cited_claims = [claims.get(alias) for alias in sentence.dossier_claims]
    cited = [*(by_alias.get(alias) for alias in sentence.facts), *cited_claims]
    if any(fact is None for fact in cited):
        return None  # an alias the request never numbered
    found = [fact for fact in cited if fact is not None]
    if tailor.unsupported_reason(text_, found, skills=skills, job_terms=terms, roles=roles) is not None:
        return None
    return found


def check_draft(
    draft: PacketDraft,
    facts: list[tailor.Fact],
    requirements: list[str],
    *,
    principal: UUID | None,
    dossier_claims: Sequence[dict[str, Any]] | None = None,
) -> tuple[dict[str, Checked], Checked]:
    """Per question and for the note: the sentences that passed both layers and the number removed. A question the
    Writer may not draft gets nothing, whatever it wrote. The one verify.entail call covers every sentence that passed
    layer 1 across all answers and the note, and fails closed. `dossier_claims` (a campaign packet only) are d1..dK; a
    sentence citing one is checked against its facts and those claim texts together."""
    by_alias = {f"f{n}": fact for n, fact in enumerate(facts, 1)}
    claim_facts = {
        f"d{n}": tailor.Fact(id=f"d{n}", kind="note", statement=" ".join(str(c["text"]).split()))
        for n, c in enumerate(dossier_claims or [], 1)
    }
    skills = [str(f.attributes.get("name") or f.statement) for f in facts if f.kind == "skill"]
    roles = [f for f in facts if f.kind == "role"]
    allowed = {q.id for q in PACKET_QUESTIONS if q.draft}
    seen: set[str] = set()
    groups: dict[str, list[Sentence]] = {}
    for item in draft.answers:
        if item.question_id in allowed and item.question_id not in seen and item.status == "drafted":
            seen.add(item.question_id)
            groups[item.question_id] = item.sentences
    groups["note"] = draft.note
    caps = {key: MAX_NOTE_SENTENCES if key == "note" else MAX_SENTENCES_PER_ANSWER for key in groups}
    results = {key: Checked([]) for key in groups}
    pending: list[tuple[str, Sentence, list[tailor.Fact]]] = []
    for key, sentences in groups.items():
        for n, sentence in enumerate(sentences):
            cited = _layer_one(sentence, by_alias, skills, requirements, roles, claim_facts) if n < caps[key] else None
            if cited is None:
                results[key].removed += 1
            else:
                pending.append((key, sentence, cited))
    verdicts = tailor.entailed([tailor.Claim(s.text.strip(), cited) for _, s, cited in pending], principal=principal)
    for (key, sentence, cited), entailed in zip(pending, verdicts, strict=True):
        if entailed:
            results[key].sentences.append(sentence)
        else:
            results[key].removed += 1
    note = results.pop("note")
    return results, note


def drop_unusable(checked: dict[str, Checked], note: Checked, facts: list[tailor.Fact], usable: set[str]) -> None:
    """Remove, and count as removed, every kept sentence that cites a fact no longer in `usable` (05 fix A2). Changes
    the arguments in place."""
    by_alias = {f"f{n}": fact for n, fact in enumerate(facts, 1)}
    for group in (*checked.values(), note):
        kept = [s for s in group.sentences if all(by_alias[alias].id in usable for alias in s.facts)]
        group.removed += len(group.sentences) - len(kept)
        group.sentences = kept


def _fact_ids(sentences: list[Sentence], by_alias: dict[str, tailor.Fact]) -> list[str]:
    return list(dict.fromkeys(by_alias[alias].id for s in sentences for alias in s.facts))


def _claim_indexes(sentences: list[Sentence]) -> list[int]:
    """Positions (0-based) in the campaign's dossier of the claims the kept sentences cite."""
    return sorted({int(alias[1:]) - 1 for s in sentences for alias in s.dossier_claims})


def assemble(
    checked: dict[str, Checked], note: Checked, facts: list[tailor.Fact], *, with_claims: bool = False
) -> tuple[list[dict[str, Any]], str, int]:
    """The six answers in the fixed order, the note cut at a sentence boundary to MAX_NOTE, and how many sentences the
    note lost (kept in the log: the table has no column for it)."""
    by_alias = {f"f{n}": fact for n, fact in enumerate(facts, 1)}
    answers: list[dict[str, Any]] = []
    for q in PACKET_QUESTIONS:
        got = checked.get(q.id)
        kept = got.sentences if got else []
        answer: dict[str, Any] = {
            "question_id": q.id,
            "question": q.text,
            "status": "drafted" if kept else "candidate_must_answer",
            "answer": " ".join(s.text.strip() for s in kept),
            "fact_ids": _fact_ids(kept, by_alias),
            "removed_sentences": got.removed if got else 0,
        }
        if with_claims:  # only a campaign packet carries the dossier citations (Phase 5 packets are unchanged)
            answer["dossier_claim_indexes"] = _claim_indexes(kept)
        answers.append(answer)
    body, dropped = "", note.removed
    for sentence in note.sentences:
        joined = f"{body} {sentence.text.strip()}".strip()
        if len(joined) > MAX_NOTE:
            dropped += 1
        else:
            body = joined
    return answers, body, dropped


# ---- request, draft, read ----------------------------------------

ONE = text(
    """SELECT id, job_id, tailored_resume_id, answers, cover_note, status::text AS status, created_at
         FROM application_packets WHERE user_id = :u AND id = :id"""
)
CURRENT = text(
    """SELECT id, job_id, tailored_resume_id, answers, cover_note, status::text AS status, created_at
         FROM application_packets WHERE user_id = :u AND status <> 'superseded' AND campaign_id IS NULL
          AND (CAST(:job AS bigint) IS NULL OR job_id = :job) ORDER BY created_at DESC, id LIMIT 50"""
)
TAILORED = text("""SELECT status::text AS status, job_id FROM tailored_resumes WHERE user_id = :u AND id = :id""")
SUPERSEDE = text(
    """UPDATE application_packets SET status = 'superseded'
        WHERE user_id = :u AND job_id = :j AND campaign_id IS NULL AND status <> 'superseded'"""
)
# a campaign's packet supersedes the campaign's own previous one, never the job's campaign-less packet
SUPERSEDE_CAMPAIGN = text(
    """UPDATE application_packets SET status = 'superseded'
        WHERE user_id = :u AND campaign_id = :c AND status <> 'superseded'"""
)
INSERT = text(
    """INSERT INTO application_packets (user_id, job_id, tailored_resume_id, campaign_id)
       VALUES (:u, :j, :t, :c) RETURNING id"""
)
LOCK = text("SELECT user_id, job_id, status::text AS status FROM application_packets WHERE id = :id FOR UPDATE")
STORE = text(
    """UPDATE application_packets SET answers = CAST(:answers AS jsonb), cover_note = :note, status = 'ready'
        WHERE id = :id"""
)
DEAD = text("SELECT 1 FROM queue_jobs WHERE unique_key = :key AND status = 'dead'")


class AnswerView(BaseModel):
    question_id: str
    question: str
    status: Literal["drafted", "candidate_must_answer"]
    answer: str  # empty when the candidate answers it
    fact_ids: list[UUID]  # the confirmed facts the answer rests on
    removed_sentences: int  # sentences of this answer that failed a check and were removed


class PacketView(BaseModel):
    id: UUID
    job_id: int
    job_title: str
    company_name: str
    status: PacketStatus  # failed is derived from a dead queue job
    tailored_resume_id: UUID | None
    answers: list[AnswerView]
    cover_note: str | None
    removed_sentences: int  # all removed from the answers (the note's count is in the log only)
    created_at: datetime


def request(
    conn: Connection,
    user_id: UUID,
    job_id: int,
    tailored_resume_id: UUID | None = None,
    *,
    campaign_id: UUID | None = None,
) -> PacketView:
    """Start a packet for a job: the old current one is superseded and the new row waits in `draft` for the worker. With
    `campaign_id` the packet belongs to that campaign (the campaign's previous packet is superseded, not the job's
    campaign-less one) and no apply.packet job is enqueued: the campaign's packet_draft node drafts it, with the dossier
    claims, so a job that drafts without them can never run first and turn the node's draft into a no-op."""
    tailor.open_job(conn, job_id)
    tailor.require_ai(conn, user_id)
    if tailored_resume_id is not None:
        found = conn.execute(TAILORED, {"u": user_id, "id": tailored_resume_id}).one_or_none()
        if found is None or found.job_id != job_id:
            raise ApiError(
                404, "not_found", "No such resource."
            )  # another person's, another job's and unknown look alike
        if found.status != "ready":
            raise ApiError(409, "conflict", "That tailored resume is not ready.", {"state": "not_ready"})
    else:
        tailored_resume_id = tailor.latest_ready_for_job(conn, user_id, job_id)
    tailor.rate_limited(conn, user_id, "application_packets", PACKETS_PER_DAY)
    if campaign_id is None:
        conn.execute(SUPERSEDE, {"u": user_id, "j": job_id})
    else:
        conn.execute(SUPERSEDE_CAMPAIGN, {"u": user_id, "c": campaign_id})
    created = conn.execute(INSERT, {"u": user_id, "j": job_id, "t": tailored_resume_id, "c": campaign_id}).scalar_one()
    if campaign_id is None:
        enqueue(
            conn,
            JOB_KIND,
            {"packet_id": str(created)},
            unique_key=f"{JOB_KIND}:{created}",
            queue=QUEUE,
            user_id=user_id,
        )
    return get(conn, user_id, created)


def draft(conn: Connection, payload: dict[str, Any], *, dossier_claims: list[dict[str, Any]] | None = None) -> None:
    """The handler of apply.packet. Consent or AI off, or an answer that stays invalid, is NonRetryable (the queue
    marks the job dead and the packet reads as failed); a provider that did not answer is retried by the queue. A packet
    that is not `draft` is left alone: no model call is made. A campaign's node passes the dossier's claims
    (DATA_MODEL claim shape: at least `text`) so the Writer may cite them as d1..dK."""
    packet_id = UUID(payload["packet_id"])
    row = conn.execute(LOCK, {"id": packet_id}).one_or_none()
    if row is None or row.status != "draft":
        return  # gone, superseded by a newer request, or finished by an overlapping run
    user_id = row.user_id
    try:
        trust.require_consent(conn, user_id, "ai_processing")
    except trust.ConsentRequired:
        raise NonRetryable("consent_required") from None
    facts = [tailor.to_fact(f) for f in profile.usable_facts(conn, user_id)]
    job = ingest.get_job(conn, row.job_id) or {}
    requirements = [r["text"] for r in job.get("requirements", [])]
    conn.commit()  # no transaction stays open while the models answer
    try:
        answer = cast(
            PacketDraft,
            complete(
                TASK,
                PacketDraft,
                build_messages(facts, requirements, dossier_claims=dossier_claims),
                principal=user_id,
                prompt_version=PROMPT_VERSION,
            ),
        )
        checked, note = check_draft(answer, facts, requirements, principal=user_id, dossier_claims=dossier_claims)
    except AIDisabled:
        raise NonRetryable("ai_disabled") from None
    except SchemaInvalid:
        raise NonRetryable("schema_invalid") from None
    stored = conn.execute(LOCK, {"id": packet_id}).one_or_none()
    if stored is None or stored.status != "draft":
        return  # a newer request superseded this one while the models answered
    # the models took a while: a fact cited above may have been retired since, so re-read before anything is stored (A2)
    usable = {str(f["id"]) for f in profile.usable_facts(conn, user_id)}
    drop_unusable(checked, note, facts, usable)
    answers, cover_note, note_removed = assemble(checked, note, facts, with_claims=bool(dossier_claims))
    conn.execute(STORE, {"id": packet_id, "answers": json.dumps(answers), "note": cover_note})
    log.event(
        "apply.packet",
        level=logging.INFO,
        packet_id=packet_id,
        status="ready",
        drafted=sum(a["status"] == "drafted" for a in answers),
        removed=sum(a["removed_sentences"] for a in answers),
        note_removed=note_removed,
    )


def _view(conn: Connection, row: Any) -> PacketView:
    job = ingest.get_job(conn, row.job_id) or {}
    status: PacketStatus = row.status
    if status == "draft" and conn.execute(DEAD, {"key": f"{JOB_KIND}:{row.id}"}).first() is not None:
        status = "failed"
    answers = [AnswerView(**a) for a in row.answers]
    return PacketView(
        id=row.id,
        job_id=row.job_id,
        job_title=job.get("title", ""),
        company_name=job.get("company_name", ""),
        status=status,
        tailored_resume_id=row.tailored_resume_id,
        answers=answers,
        cover_note=row.cover_note,
        removed_sentences=sum(a.removed_sentences for a in answers),
        created_at=row.created_at,
    )


def get(conn: Connection, user_id: UUID, packet_id: UUID) -> PacketView:
    row = conn.execute(ONE, {"u": user_id, "id": packet_id}).one_or_none()
    if row is None:
        raise ApiError(404, "not_found", "No such resource.")
    return _view(conn, row)


def current(conn: Connection, user_id: UUID, job_id: int | None = None) -> list[PacketView]:
    """The person's current packets (never a superseded one), newest first; `job_id` limits them to one job."""
    return [_view(conn, row) for row in conn.execute(CURRENT, {"u": user_id, "job": job_id})]


def current_for_job(conn: Connection, user_id: UUID, job_id: int) -> PacketView | None:
    found = current(conn, user_id, job_id)
    return found[0] if found else None


# ---- the PDF ----------------------------------------


def pdf(conn: Connection, user_id: UUID, packet_id: UUID) -> bytes:
    """The packet as one PDF: the note, each question with its answer (or "You answer this"), then the resume with the
    approved edits of the packet's tailored resume applied, else the base resume. Raises tailor.UnsupportedCharacters
    for a character the PDF cannot draw; nothing is stored."""
    view = get(conn, user_id, packet_id)
    if view.status != "ready":
        raise ApiError(409, "conflict", "This packet is not ready.", {"state": "not_ready"})
    doc = (
        tailor.export_doc(conn, user_id, view.tailored_resume_id)
        if view.tailored_resume_id is not None
        else tailor.base_doc(conn, user_id)
    )
    title = f"{view.job_title} at {view.company_name}".strip() if view.job_title else "Application packet"
    blocks = [tailor.Block(heading=f"Application packet: {title}", level=0)]
    if view.cover_note:
        blocks.append(tailor.Block(heading="Note", lines=(view.cover_note,)))
    blocks.append(
        tailor.Block(
            heading="Answers",
            lines=tuple(
                line
                for a in view.answers
                for line in (a.question, a.answer if a.status == "drafted" and a.answer else YOU_ANSWER)
            ),
        )
    )
    return tailor.render_pdf([*blocks, *tailor.resume_blocks(doc)])
