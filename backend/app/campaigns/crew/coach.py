"""The Coach (CMP-05; AI_SYSTEM 6.9): interview preparation from the evidence gaps and the company dossier.

The model proposes likely questions, each tied to one requirement of the job, with the candidate's confirmed facts to
tell as a story and a one-line framing, and a few questions for the candidate to ask the company, each tied to a
dossier claim.
Nothing is kept until code agrees:
  - a question maps to a known requirement (r1..) and cites real facts (f1..); a story with no fact is removed,
  - a gap (a requirement judged partly met, not met or without evidence) may have no fact, but its framing may claim
    nothing the facts lack: layer 1 of the tailoring verifier plus verify.entail, failing closed, with a stand-in fact
    that says the record shows no experience of that requirement, so "I led X" fails and "I have not used X yet" passes,
  - a question to ask the company cites a dossier claim (d1..) and repeats no number or name the claim lacks.
Every removal is counted by reason; the node stores the counts beside the pack.
"""

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.platform.llm import complete
from app.tailor import api as tailor

from . import common

TASK = "coach.prep"
PROMPT = (Path(__file__).parent.parent / "prompts" / "coach_prep.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]
GAP_VERDICTS = frozenset({"partly_met", "not_met", "no_evidence"})
MAX_QUESTIONS = 12  # assumption: questions in one pack
MAX_ASK_THEM = 5  # assumption
MAX_TEXT = 500  # assumption: characters of a question or framing
# Plain coaching words, given to layer 1 as if a fact held them, so a framing is not removed for saying "I have not".
COACHING = (
    "Be honest that you have not done this yet. Describe a related thing you have done and how you would learn "
    "the rest. "
    "Tell the story of what you did, what you decided and what happened."
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Comments, not docstrings: a docstring becomes prose in the JSON schema the provider is sent.
class PrepQuestion(_Strict):
    text: str
    requirement: str  # alias r1..
    facts: list[str]  # aliases f1..: the stories to tell
    gap: bool
    framing: str  # one line on how to answer


class AskThem(_Strict):
    text: str
    dossier_claims: list[str]  # aliases d1..


class PrepPack(_Strict):
    questions: list[PrepQuestion]
    ask_them: list[AskThem]


def build_messages(
    facts: Sequence[tailor.Fact],
    requirements: Sequence[tailor.Requirement],
    verdicts: Mapping[int, str],
    claims: Sequence[str],
) -> list[dict[str, str]]:
    """The coach.prep request: fact statements, requirement texts with their verdicts, dossier claim texts. No name, no
    email, no document text, no id. Pure."""
    lines = ['Facts about the candidate, one per line as "[f1] kind: text".']
    lines += [f"[f{n}] {fact.kind}: {tailor.fact_text(fact)}" for n, fact in enumerate(facts, 1)]
    lines += ["", 'Company facts, one per line as "[d1] text".']
    lines += [f"[d{n}] {' '.join(text.split())}" for n, text in enumerate(claims, 1)]
    lines += ["", 'Requirements, one per line as "[r1] (importance, verdict) text".', "BEGIN JOB TEXT"]
    for n, requirement in enumerate(requirements, 1):
        verdict = verdicts.get(requirement.id, "not assessed").replace("_", " ")
        lines.append(f"[r{n}] ({requirement.importance}, {verdict}) {' '.join(requirement.text.split())}")
    lines += ["END JOB TEXT"]
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def draft(
    facts: Sequence[tailor.Fact],
    requirements: Sequence[tailor.Requirement],
    verdicts: Mapping[int, str],
    claims: Sequence[str],
    *,
    principal: UUID | None,
) -> PrepPack:
    """One coach.prep call. Raises what the gateway raises (AIDisabled, SchemaInvalid ...)."""
    return cast(
        PrepPack,
        complete(
            TASK,
            PrepPack,
            build_messages(facts, requirements, verdicts, claims),
            principal=principal,
            prompt_version=PROMPT_VERSION,
        ),
    )


def _note(text: str) -> tailor.Fact:
    return tailor.Fact(id="note", kind="note", statement=text)


def validate(
    pack: PrepPack,
    *,
    facts: Sequence[tailor.Fact],
    requirements: Sequence[tailor.Requirement],
    verdicts: Mapping[int, str],
    claims: Sequence[str],
    skills: Sequence[str],
    job_terms: Sequence[str],
    principal: UUID | None,
) -> tuple[PrepPack, dict[str, int]]:
    """(the pack with only what passed, removed_by_reason). `gap` is set by code from the stored verdict, whatever the
    model said. One verify.entail call covers every framing that passed layer 1."""
    removed: dict[str, int] = {}
    fact_table = common.aliases("f", facts)
    req_table = common.aliases("r", requirements)
    claim_table = {k: _note(v) for k, v in common.aliases("d", claims).items()}
    coaching = _note(COACHING)
    pending: list[tuple[PrepQuestion, list[tailor.Fact]]] = []
    seen: set[str] = set()
    for position, item in enumerate(pack.questions):
        requirement = req_table.get(item.requirement)
        cited = common.resolve(item.facts, fact_table)
        text, framing = item.text.strip(), item.framing.strip()
        if position >= MAX_QUESTIONS:
            common.count(removed, "too_many_questions")
        elif not text or not framing or len(text) > MAX_TEXT or len(framing) > MAX_TEXT:
            common.count(removed, "bad_length")
        elif requirement is None:
            common.count(removed, "unknown_requirement")
        elif cited is None:
            common.count(removed, "unknown_alias")
        elif text.casefold() in seen:
            common.count(removed, "duplicate")
        else:
            gap = verdicts.get(requirement.id, "no_evidence") in GAP_VERDICTS
            if not cited and not gap:
                common.count(removed, "story_without_fact")
                continue
            support = [*cited, coaching]
            if gap:
                support.append(_note(f"The record shows no confirmed experience of: {requirement.text}"))
            if reason := tailor.unsupported_reason(framing, support, skills=skills, job_terms=job_terms):
                common.count(removed, f"framing_{reason}")
                continue
            seen.add(text.casefold())
            pending.append((item.model_copy(update={"gap": gap}), support))
    verdict_list = tailor.entailed([(q.framing.strip(), support) for q, support in pending], principal=principal)
    questions: list[PrepQuestion] = []
    for (item, _), ok in zip(pending, verdict_list, strict=True):
        if ok:
            questions.append(item)
        else:
            common.count(removed, "framing_not_entailed")
    ask: list[AskThem] = []
    for position, ask_item in enumerate(pack.ask_them):
        cited_claims = common.resolve(ask_item.dossier_claims, claim_table)
        text = ask_item.text.strip()
        if position >= MAX_ASK_THEM:
            common.count(removed, "too_many_ask_them")
        elif not text or len(text) > MAX_TEXT:
            common.count(removed, "bad_length")
        elif not ask_item.dossier_claims:
            common.count(removed, "ask_without_claim")
        elif cited_claims is None:
            common.count(removed, "unknown_claim")
        elif reason := tailor.unsupported_reason(text, [*cited_claims, coaching], skills=skills, job_terms=job_terms):
            common.count(removed, f"ask_{reason}")
        else:
            ask.append(ask_item)
    return PrepPack(questions=questions, ask_them=ask), removed


def to_output(
    pack: PrepPack,
    *,
    facts: Sequence[tailor.Fact],
    requirements: Sequence[tailor.Requirement],
    removed_by_reason: dict[str, int],
) -> dict[str, Any]:
    """The `interview_prep` step output (INDEX Step outputs): ids, indexes and validated text only."""
    fact_table = common.aliases("f", facts)
    req_table = common.aliases("r", requirements)
    return {
        "questions": [
            {
                "text": q.text.strip(),
                "requirement_id": req_table[q.requirement].id,
                "fact_ids": [fact_table[a].id for a in q.facts],
                "gap": q.gap,
                "framing": q.framing.strip(),
            }
            for q in pack.questions
        ],
        "ask_them": [
            {"text": a.text.strip(), "claim_indexes": [int(alias[1:]) - 1 for alias in a.dossier_claims]}
            for a in pack.ask_them
        ],
        "removed_by_reason": dict(sorted(removed_by_reason.items())),
    }
