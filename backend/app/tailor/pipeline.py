"""tailor.propose and verify.entail: the two model tasks of tailoring, and the pipeline that checks the first one's
answer (AI_SYSTEM 4.7, TLR-01, TLR-02, invariant 3).

propose_and_verify sends the candidate's facts and the job's requirements, receives edits, and checks every edit before
anything stores or shows it: layer 1 is code (verifier.py), layer 2 is one verify.entail call for the rephrases that
passed layer 1, on a different model and failing closed. What comes back is every edit, passed or discarded, each with
the reason it was discarded; a discarded edit is stored and counted, and its text is never shown (TLR-02).

The messages hold fact statements and requirement texts and nothing else: no id, no name, no document text, and an
email address or link inside a statement is replaced. The job's text is data, not instructions (AI_SYSTEM 11): it sits
between two marker lines, each requirement on one line, and whatever the model proposes still has to pass layer 1.
"""

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, cast
from uuid import UUID

from app.platform import log
from app.platform.llm import SchemaInvalid, complete

from . import resume, verifier
from .resume import Fact
from .schemas import EditItem, EditList, EditListSafe, EntailSet
from .verifier import Context, Requirement

PROMPTS = Path(__file__).parent / "prompts"
PROPOSE_TASK, ENTAIL_TASK = "tailor.propose", "verify.entail"
PROPOSE_PROMPT = (PROMPTS / "tailor_propose.md").read_text(encoding="utf-8")
ENTAIL_PROMPT = (PROMPTS / "verify_entail.md").read_text(encoding="utf-8")
# The template's content hash (AI_SYSTEM 2.2), in the 12-character form the eval runner uses.
PROPOSE_VERSION = hashlib.sha256(PROPOSE_PROMPT.encode()).hexdigest()[:12]
ENTAIL_VERSION = hashlib.sha256(ENTAIL_PROMPT.encode()).hexdigest()[:12]

VERDICTS = ("met", "partly_met", "not_met", "no_evidence")
REMOVED = "[removed]"
SECTION_OF_KIND = {
    "role": "experience",
    "achievement": "experience",
    "project": "projects",
    "skill": "skills",
    "certification": "certifications",
    "education": "education",
}

_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_LINK = re.compile(r"(?:https?://|www\.)[^\s<>\"']+")
_LINK_TRAILING = ".,;:!?)]}"  # punctuation that follows a link without being part of it


@dataclass(frozen=True)
class VerifiedEdit:
    """One proposed edit after both layers, in the shape resume_edits stores. Texts are as they would appear on the
    resume. For a reorder, before_text and after_text hold one item per line and supporting_fact_ids lists the facts in
    their new order; for an add_from_fact before_text is empty; for a remove after_text is empty."""

    ordinal: int  # 1..n in the order the model gave them
    op: str  # rephrase | reorder | remove | add_from_fact
    section: str  # derived from the facts, not taken from the model
    target_fact_id: str | None
    before_text: str
    after_text: str
    supporting_fact_ids: tuple[str, ...]
    verifier_status: str  # passed | discarded
    discard_reason: str | None  # an edit_discard_reason; set exactly when discarded


@dataclass(frozen=True)
class Proposal:
    edits: list[VerifiedEdit]
    prompt_version: str  # of tailor.propose; verify.entail's own version is on its llm_calls rows


class Claim(NamedTuple):
    """A new text and the facts it rests on, for verify.entail."""

    text: str
    facts: Sequence[Fact]


@dataclass
class _Draft:
    op: str
    section: str
    target_fact_id: str | None
    before_text: str
    after_text: str
    supporting_fact_ids: tuple[str, ...]
    reason: str | None = None


# ---- what the model is sent ----------------------------------------


def _one_line(text: str) -> str:
    """A text on one line, with an email address or a link replaced (the second guard: Phase 3 masks contact details
    before any fact exists)."""

    def remove_link(found: "re.Match[str]") -> str:
        link = found.group()
        return REMOVED + link[len(link.rstrip(_LINK_TRAILING)) :]

    return " ".join(_LINK.sub(remove_link, _EMAIL.sub(REMOVED, text)).split())


def fact_text(fact: Fact) -> str:
    """The text a fact has in a request: a role as its header and dates, anything else as it is on the resume."""
    return _one_line(resume.role_line(fact) if fact.kind == "role" else resume.item_text(fact))


def build_messages(
    facts: Sequence[Fact],
    requirements: Sequence[Requirement],
    verdicts: Mapping[int, str],
    *,
    allow_rephrase: bool,
) -> list[dict[str, str]]:
    """The tailor.propose request. Facts are f1..fN in the order given (profile.api.usable_facts order) and
    requirements r1..rM in theirs; verdicts maps a requirement id to what the matching step found."""
    ctx = verifier.build_context(facts, requirements)
    lines = [
        'Facts about the candidate, one per line as "[f1] kind: text". "of f1" names the role a bullet sits under; '
        '"not on the resume" marks a confirmed fact the resume does not show yet; "background only" marks one that may '
        "support an edit but is never edited or added."
    ]
    for alias, fact in ctx.facts.items():
        parent = ctx.aliases.get(fact.parent_fact_id) if fact.parent_fact_id else None
        label = fact.kind + (f" of {parent}" if parent else "")
        if fact.id not in ctx.shown:
            label += " (not on the resume)" if fact.kind in verifier.ADDABLE else " (background only)"
        lines.append(f"[{alias}] {label}: {fact_text(fact)}")
    lines += [
        "",
        'Requirements of the job, one per line as "[r1] (importance, verdict) text". The verdict is what an earlier '
        "step found for that requirement.",
        "BEGIN JOB TEXT",
    ]
    for number, requirement in enumerate(requirements, 1):
        said = verdicts.get(requirement.id)
        verdict = f", {said.replace('_', ' ')}" if isinstance(said, str) and said in VERDICTS else ""
        lines.append(f"[r{number}] ({requirement.importance}{verdict}) {_one_line(requirement.text)}")
    operations = ("rephrase, " if allow_rephrase else "") + "reorder, remove, add_from_fact"
    lines += ["END JOB TEXT", "", f"Operations allowed in this request: {operations}."]
    return [{"role": "system", "content": PROPOSE_PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def build_entail_messages(claims: Sequence[Claim]) -> list[dict[str, str]]:
    """The verify.entail request: each claim's new text and the statements of the facts it rests on, no ids."""
    blocks = [
        "\n".join(
            [
                f"[e{number}]",
                f"New text: {_one_line(claim.text)}",
                "Facts:",
                *(f"- {fact_text(f)}" for f in claim.facts),
            ]
        )
        for number, claim in enumerate(claims, 1)
    ]
    return [{"role": "system", "content": ENTAIL_PROMPT}, {"role": "user", "content": "\n\n".join(blocks)}]


# ---- layer 2 ----------------------------------------


def entailed(items: Sequence[Claim | tuple[str, Sequence[Fact]]], *, principal: UUID | None) -> list[bool]:
    """Layer 2: for each (new text, supporting facts), does verify.entail say the facts support every statement of the
    text? One gateway call for all of them. It fails closed: an item the answer leaves out, names twice with two
    answers, or an answer that is invalid twice is False. AIDisabled and ProviderUnavailable propagate: with no
    checker nothing may pass. A packet's sentences use this too (05-05).
    """
    # ponytail: one call for every claim; the 800-token output cap fits about 50 verdicts, and a longer list that
    # comes back cut off is invalid, so every claim fails closed. Split the call when a caller sends more.
    claims = [Claim(*item) for item in items]
    if not claims:
        return []
    try:
        answer = cast(
            EntailSet,
            complete(
                ENTAIL_TASK,
                EntailSet,
                build_entail_messages(claims),
                principal=principal,
                prompt_version=ENTAIL_VERSION,
            ),
        )
    except SchemaInvalid:
        return [False] * len(claims)
    said: dict[str, bool] = {}
    for item in answer.items:
        said[item.edit] = said.get(item.edit, True) and item.entailed  # an answer that contradicts itself is a no
    return [said.get(f"e{number}", False) for number in range(1, len(claims) + 1)]


# ---- resolving an edit against the facts ----------------------------------------


def _section(fact: Fact | None, given: str) -> str:
    return SECTION_OF_KIND.get(fact.kind, given) if fact else given


def _ids(*groups: Sequence[Fact | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(fact.id for group in groups for fact in group if fact is not None))


def _lines(facts: Sequence[Fact]) -> str:
    return "\n".join(resume.item_text(fact) for fact in facts)


def _resolve_reorder(item: EditItem, ctx: Context) -> _Draft | None:
    listed = [fact for fact in (ctx.facts.get(alias) for alias in item.order) if fact is not None]
    group = verifier.reorder_group(item, ctx)
    if group is not None and tuple(item.order) == group:
        return None  # the order it already has
    current = [ctx.facts[alias] for alias in group] if group is not None else listed
    section = _section(listed[0] if listed else None, item.section)
    return _Draft("reorder", section, None, _lines(current), _lines(listed), _ids(listed))


def _resolve(item: EditItem, ctx: Context) -> _Draft | None:
    """The edit in the shape of a stored row, or None when it changes nothing: an edit that does nothing is not shown
    and is not counted as discarded. An alias the request never numbered resolves to nothing; layer 1 then discards
    the edit."""
    if item.op == "reorder":
        return _resolve_reorder(item, ctx)
    after = resume.line_text(item.after_text)
    if item.op == "add_from_fact":
        fact = ctx.facts.get(item.supporting[0])
        return _Draft("add_from_fact", _section(fact, item.section), None, "", after, _ids([fact]))
    target = ctx.facts.get(item.target or "")
    before = resume.item_text(target) if target is not None else ""
    named = [ctx.facts.get(alias) for alias in item.supporting]
    target_id = target.id if target is not None else None
    # the target is always among the facts the new words rest on, and is listed first
    if item.op == "remove":
        return _Draft("remove", _section(target, item.section), target_id, before, "", _ids([target], named))
    if target is not None and after == before:
        return None
    return _Draft("rephrase", _section(target, item.section), target_id, before, after, _ids([target], named))


def propose_and_verify(
    facts: Sequence[Fact],
    requirements: Sequence[Requirement],
    verdicts: Mapping[int, str],
    *,
    principal: UUID | None,
    allow_rephrase: bool,
) -> Proposal:
    """Ask tailor.propose for edits and check each of them. Raises the gateway's AIDisabled, ProviderUnavailable and
    SchemaInvalid (an answer that is invalid twice, including two edits on one target): the caller marks the tailored
    resume failed or lets the queue retry. With allow_rephrase False the model is not offered a rephrase and an answer
    with one is invalid. Edits come back in the model's order, ordinals 1..n, passed and discarded alike."""
    ctx = verifier.build_context(facts, requirements)
    schema = EditList if allow_rephrase else EditListSafe
    answer = cast(
        EditList | EditListSafe,
        complete(
            PROPOSE_TASK,
            schema,
            build_messages(facts, requirements, verdicts, allow_rephrase=allow_rephrase),
            principal=principal,
            prompt_version=PROPOSE_VERSION,
        ),
    )
    drafts: list[_Draft] = []
    unchanged = 0
    for item in answer.edits:
        draft = _resolve(item, ctx)
        if draft is None:
            unchanged += 1
            continue
        draft.reason = verifier.check(item, ctx)
        drafts.append(draft)
    surviving = [draft for draft in drafts if draft.reason is None and draft.op == "rephrase"]
    claims = [Claim(draft.after_text, [ctx.by_id[i] for i in draft.supporting_fact_ids]) for draft in surviving]
    for draft, supported in zip(surviving, entailed(claims, principal=principal), strict=True):
        if not supported:
            draft.reason = "not_entailed"
    edits = [
        VerifiedEdit(
            number,
            d.op,
            d.section,
            d.target_fact_id,
            d.before_text,
            d.after_text,
            d.supporting_fact_ids,
            "discarded" if d.reason else "passed",
            d.reason,
        )
        for number, d in enumerate(drafts, 1)
    ]
    by_reason = {f"discarded_{r}": sum(d.reason == r for d in drafts) for r in (*verifier.REASONS, "not_entailed")}
    log.event(
        "tailor.verified",
        passed=sum(d.reason is None for d in drafts),
        discarded=sum(d.reason is not None for d in drafts),
        unchanged=unchanged,
        **by_reason,
    )
    return Proposal(edits, PROPOSE_VERSION)
