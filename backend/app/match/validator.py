"""The citation validator (AI_SYSTEM 4.4, MAT-05, invariant 2): code that holds a model's verdicts to the request it was
sent. The model's answer is untrusted until it passes here, and the database trigger checks the stored rows again.

validate(answer, request, career_months=...) applies six rules, in order, and returns on the first rejection:

  1 coverage      exactly one verdict per requirement alias sent; none unknown, none repeated
  2 existence     every cited fact alias is one the request carried (so it is this person's, confirmed and current)
  3 spans         each requirement's posting text at its span is still its stored text; each cited fact still has its
                  source span, or the event that confirmed it
  4 cardinality   met, partly_met and not_met cite at least one fact; no_evidence cites none
  5 numeric       met on a requirement that asks for N years is refused when N years exceeds the whole career
  6 lexical       met on a skill requirement whose cited facts share no term with it is ACCEPTED but marked weak_fit:
                  the job is held back (reason weak_fit) and sampled for the weekly human audit

A rejection carries the rule number and a short code, never any text from the answer. What the rules cannot catch, a
wrong judgement about a real fact, is what the evidence-faithfulness gold set measures. No I/O, nothing logged.
"""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from .evidence import EvidenceRequest

MONTHS_PER_YEAR = 12


class Verdict(BaseModel):
    """One requirement's verdict: the shape of AI_SYSTEM 4.4. Only shapes live here; the rules below judge them."""

    model_config = ConfigDict(extra="forbid")
    requirement: str  # an alias, r1..rM
    verdict: Literal["met", "partly_met", "not_met", "no_evidence"]
    facts: list[str]  # aliases, f1..fN


class VerdictSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdicts: list[Verdict]


@dataclass(frozen=True)
class EvidenceRow:
    """A verdict mapped back to stored ids. The importance and the requirement's words ride along so that ranking and
    the summary need no second lookup; only requirement_id, verdict and fact_ids are stored in match_evidence."""

    requirement_id: int
    importance: str  # required | preferred
    text: str
    verdict: str
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class Accepted:
    rows: tuple[EvidenceRow, ...]  # in requirement order
    weak_fit: bool  # rule 6


@dataclass(frozen=True)
class Rejected:
    rule: int
    code: str


# Words that carry no skill: articles and joiners, and the filler of a requirement sentence ("strong experience with").
# assumption: a short list, to be grown if the weekly audit shows filler passing as support.
_IGNORED = frozenset(
    """a an and are as at be by for from in into is it of on or the to with without within across over per via
    experience experienced experiences years year strong solid good great excellent proven demonstrated knowledge
    proficiency proficient ability able skills skill working work worked understanding familiarity familiar hands
    plus preferred required using use including include related relevant background expertise deep basic advanced
    level minimum least more than etc""".split()
)
_TERM = re.compile(r"[a-z0-9]+(?:[+#]+)?")  # c++ and c# stay whole


def _terms(text: str) -> set[str]:
    """Lower-case terms without the ignored words, a trailing plural s dropped, so tests and test agree."""
    found = (t for t in _TERM.findall(text.lower()) if t not in _IGNORED)
    return {t[:-1] if len(t) > 3 and t.endswith("s") else t for t in found}


def _coverage(answer: VerdictSet, request: "EvidenceRequest") -> Rejected | None:
    seen: set[str] = set()
    for verdict in answer.verdicts:
        if verdict.requirement not in request.requirements:
            return Rejected(1, "requirement_unknown")
        if verdict.requirement in seen:
            return Rejected(1, "requirement_repeated")
        seen.add(verdict.requirement)
    return None if seen == set(request.requirements) else Rejected(1, "requirement_missing")


def _existence(answer: VerdictSet, request: "EvidenceRequest") -> Rejected | None:
    unknown = any(alias not in request.facts for verdict in answer.verdicts for alias in verdict.facts)
    return Rejected(2, "fact_unknown") if unknown else None


def _spans(answer: VerdictSet, request: "EvidenceRequest") -> Rejected | None:
    if any(req.at_span != req.text for req in request.requirements.values()):
        return Rejected(3, "requirement_changed")
    for verdict in answer.verdicts:
        for alias in verdict.facts:
            fact = request.facts[alias]
            if fact.span is None and not fact.confirmed_by_event:
                return Rejected(3, "fact_unanchored")
    return None


def _cardinality(answer: VerdictSet) -> Rejected | None:
    for verdict in answer.verdicts:
        if verdict.verdict == "no_evidence" and verdict.facts:
            return Rejected(4, "no_evidence_cites")
        if verdict.verdict != "no_evidence" and not verdict.facts:
            return Rejected(4, "cites_nothing")
    return None


def _numeric(answer: VerdictSet, request: "EvidenceRequest", career_months: int) -> Rejected | None:
    # Every requirement that states a number of years is a years of experience requirement (AI_SYSTEM 4.3).
    for verdict in answer.verdicts:
        years = request.requirements[verdict.requirement].min_years
        if verdict.verdict == "met" and years is not None and years * MONTHS_PER_YEAR > career_months:
            return Rejected(5, "years_exceed_career")
    return None


def _is_weak(answer: VerdictSet, request: "EvidenceRequest") -> bool:
    for verdict in answer.verdicts:
        requirement = request.requirements[verdict.requirement]
        if verdict.verdict == "met" and requirement.kind == "skill":
            cited = " ".join(request.facts[alias].statement for alias in verdict.facts)
            if not _terms(requirement.text) & _terms(cited):
                return True
    return False


def validate(answer: VerdictSet, request: "EvidenceRequest", *, career_months: int) -> Accepted | Rejected:
    """Accepted rows in requirement order, or the first rule the answer breaks. career_months is the candidate's
    whole confirmed career in months, counted by code (EvidenceRequest.career_months)."""
    if (rejected := _coverage(answer, request)) is not None:
        return rejected
    if (rejected := _existence(answer, request)) is not None:
        return rejected
    if (rejected := _spans(answer, request)) is not None:
        return rejected
    if (rejected := _cardinality(answer)) is not None:
        return rejected
    if (rejected := _numeric(answer, request, career_months)) is not None:
        return rejected
    given = {verdict.requirement: verdict for verdict in answer.verdicts}
    rows = tuple(
        EvidenceRow(
            requirement_id=requirement.id,
            importance=requirement.importance,
            text=requirement.text,
            verdict=given[alias].verdict,
            fact_ids=tuple(dict.fromkeys(request.facts[a].id for a in given[alias].facts)),
        )
        for alias, requirement in request.requirements.items()
    )
    return Accepted(rows, _is_weak(answer, request))
