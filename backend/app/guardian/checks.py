"""Key two: the six Guardian checks (AI_SYSTEM 7.4).

Everything a check needs is read once by gather() into a frozen CheckInput; the six check functions are pure, so a
test or
the gold set can drive them with no database. run() returns one CheckResult per kind, always all six (a check that does
not apply passes with `applies: false`), so every attempt leaves six rows in the recorder.

What a caller (the crew) must hand over in `provenance` (a dict):
  fields        {field: "model" | "candidate" | "code"}  who wrote each text field; a field left out counts as "model"
  sentences     [{field, text, fact_ids, claim_indexes, events}]  every model-written sentence with what it cites
  code_parts    texts that code wrote inside a model field (the greeting and sign-off of a follow-up)
  dossier_id    the dossier whose claims `claim_indexes` point into (0-based)
  input_flags   codes for flags the inputs carried (a page that looked like an injection)
The citation check refuses a model field with any text that is neither a listed sentence nor a listed code part, so a
model cannot slip text past the check by leaving it out of the list.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.ingest import api as ingest
from app.match import api as match
from app.profile import api as profile
from app.trust import api as trust

from .budget import limits_reached
from .policy import SubjectGate

GUARDIAN_VERSION = "guardian-2026-11-a"
CHECK_KINDS = ("citations", "consent", "scope", "budget", "injection", "rate_limit")
MAX_FOLLOW_UPS = 2  # per campaign (N11)

# The reason codes of the two citation outcomes (the registered check returns one of these, or None)
CITATION_UNRESOLVED = "citation_unresolved"
UNSUPPORTED_CLAIM = "unsupported_claim"

# assumption: what only the candidate may state, and what a message must never touch
_PAY = re.compile(
    r"\b(salary|salaries|compensation|remuneration|ctc|current\s+pay|expected\s+pay|pay\s+expectations?|"
    r"notice\s+period|per\s+annum)\b|[$€£]\s?\d|\b\d[\d,.]*\s?(?:k|usd|eur|gbp)\b",
    re.IGNORECASE,
)
_PROTECTED = re.compile(
    r"\b(religio\w*|ethnicity|ethnic|race|racial|gender\s+identity|sexual\s+orientation|marital\s+status|pregnan\w*|"
    r"disabilit\w*|disabled|date\s+of\s+birth|years?\s+old|trade\s+union|political\s+(?:views|opinions?))\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d")
_PHONE_DIGITS = (8, 15)


@dataclass(frozen=True)
class CheckResult:
    kind: str
    passed: bool
    reason_code: str | None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CitedSentence:
    """One model-written sentence and what it cites: confirmed fact ids, 0-based indexes into the dossier's claims, and
    campaign event texts that code wrote."""

    field: str
    text: str
    fact_ids: tuple[str, ...] = ()
    claim_indexes: tuple[int, ...] = ()
    events: tuple[str, ...] = ()


# fn(sentences, *, facts, claims, skills, job_terms, principal) -> one reason code or None per sentence. `sentences` are
# the model-written ones with every cite resolved, `facts` the profile rows (profile.api.usable_facts shape) they cite
# and `claims` the dossier claim dicts they cite, both keyed as in the sentence.
CitationCheck = Callable[..., list[str | None]]
_citation_check: CitationCheck | None = None


def register_citation_check(fn: CitationCheck | None) -> None:
    """Fill (or with None, clear) the citation check. Importing app.campaigns registers it (INDEX decision 32)."""
    global _citation_check
    _citation_check = fn


@dataclass(frozen=True)
class CheckInput:
    action_type: str
    gate: SubjectGate
    texts: Mapping[str, str]  # field -> text, every text field of the payload
    sources: Mapping[str, str]  # field -> model | candidate | code
    recipient: str | None
    sentences: tuple[CitedSentence, ...] = ()
    code_parts: tuple[str, ...] = ()
    facts: Mapping[str, dict[str, Any]] = field(default_factory=dict)  # the cited usable facts by id
    claims: tuple[dict[str, Any], ...] = ()  # every claim of the dossier, in order
    skills: tuple[str, ...] = ()
    job_terms: tuple[str, ...] = ()
    employer_urls: tuple[str, ...] = ()
    consent: bool = True
    violations: tuple[str, ...] = ()  # hard-constraint kinds the job breaks now
    salary_floor: int | None = None
    follow_ups: int = 0  # the campaign's other follow-ups that are scheduled or sent
    campaign_exhausted: bool = False
    month_exhausted: bool = False
    input_flags: tuple[str, ...] = ()


# ---- reading the world (the only database reads) -------------------------------------------------------------------


def text_fields(action_type: str, payload: Mapping[str, Any]) -> dict[str, str]:
    """The fields of a payload that hold words, by the name a provenance uses."""
    if action_type == "send_follow_up":
        return {name: str(payload.get(name) or "") for name in ("subject", "body")}
    found = {"cover_note": str(payload.get("cover_note") or "")}
    for answer in payload.get("answers") or []:
        found[f"answers.{answer.get('question_id', answer.get('question'))}"] = str(answer.get("answer") or "")
    return {name: value for name, value in found.items() if value}


def gather(
    conn: Connection,
    action_type: str,
    payload: Mapping[str, Any],
    provenance: Mapping[str, Any],
    gate: SubjectGate,
    *,
    action_id: int | None = None,
    estimate_microusd: int = 0,
) -> CheckInput:
    """Read what the checks need. A fact that is not usable, a dossier of another campaign and a claim index out of
    range
    are simply absent, which the citation check reports as unresolved."""
    texts = text_fields(action_type, payload)
    declared = provenance.get("fields") or {}
    sources = {name: str(declared.get(name, "model")) for name in texts}
    sentences = tuple(
        CitedSentence(
            field=str(item["field"]),
            text=str(item["text"]),
            fact_ids=tuple(str(x) for x in item.get("fact_ids", ())),
            claim_indexes=tuple(int(x) for x in item.get("claim_indexes", ())),
            events=tuple(str(x) for x in item.get("events", ())),
        )
        for item in provenance.get("sentences") or []
    )
    user_id = gate.user_id
    usable = {str(row["id"]): row for row in profile.usable_facts(conn, user_id)}
    cited_ids = {fid for sentence in sentences for fid in sentence.fact_ids}
    claims: tuple[dict[str, Any], ...] = ()
    dossier_id = provenance.get("dossier_id")
    if dossier_id:
        stored = conn.execute(
            text("SELECT claims FROM company_dossiers WHERE id = :d AND campaign_id = :c AND user_id = :u"),
            {"d": UUID(str(dossier_id)), "c": gate.id, "u": user_id},
        ).scalar_one_or_none()
        claims = tuple(stored or ())
    job = ingest.get_job(conn, gate.job_id) or {}
    decision = match.decision_for_job(conn, user_id, gate.job_id)
    constraints = profile.compiled_constraints(conn, user_id)
    floor = (constraints.get("salary_floor") or {}).get("salary_floor")
    try:
        trust.require_consent(conn, user_id, "ai_processing")
        consent = True
    except trust.ConsentRequired:
        consent = False
    campaign_exhausted, month_exhausted = limits_reached(conn, gate, estimate_microusd)
    follow_ups = 0
    if action_type == "send_follow_up":
        follow_ups = int(
            conn.execute(
                text(
                    """SELECT count(*) FROM agent_actions
                       WHERE campaign_id = :c AND action_type = 'send_follow_up' AND status IN ('scheduled', 'executed')
                         AND id <> COALESCE(:self, 0)"""
                ),
                {"c": gate.id, "self": action_id},
            ).scalar_one()
        )
    return CheckInput(
        action_type=action_type,
        gate=gate,
        texts=texts,
        sources=sources,
        recipient=str(payload["to"]) if payload.get("to") else None,
        sentences=sentences,
        code_parts=tuple(str(x) for x in provenance.get("code_parts") or ()),
        facts={fid: usable[fid] for fid in cited_ids if fid in usable},
        claims=claims,
        skills=tuple(
            str(f["attributes"].get("name") or f["statement"]) for f in usable.values() if f["kind"] == "skill"
        ),
        job_terms=tuple([str(job["title"]), *(str(r["text"]) for r in job.get("requirements", []))] if job else ()),
        employer_urls=(str(job["apply_url"]),) if job.get("apply_url") else (),
        consent=consent,
        violations=tuple(decision.violated_kinds) if decision is not None and decision.outcome == "filtered" else (),
        salary_floor=None if floor is None else int(floor),
        follow_ups=follow_ups,
        campaign_exhausted=campaign_exhausted,
        month_exhausted=month_exhausted,
        input_flags=tuple(str(x) for x in provenance.get("input_flags") or ()),
    )


# ---- the six checks (pure) -----------------------------------------------------------------------------------------


def _model_fields(inp: CheckInput) -> list[str]:
    return [name for name, source in inp.sources.items() if source == "model" and inp.texts.get(name, "").strip()]


def _residue(inp: CheckInput, name: str) -> str:
    """The text of a model field once its listed sentences and code parts are taken out."""
    left = inp.texts[name]
    for sentence in inp.sentences:
        if sentence.field == name:
            if sentence.text not in left:
                return sentence.text  # a listed sentence that the field does not hold: the provenance is wrong
            left = left.replace(sentence.text, "", 1)
    for part in inp.code_parts:
        left = left.replace(part, "")
    return left.strip(" \n\t\r.,;:!?-—–")


def check_citations(inp: CheckInput, *, principal: UUID | None) -> CheckResult:
    fields = _model_fields(inp)
    if not fields:
        return CheckResult("citations", True, None, {"applies": False})
    if _citation_check is None:
        return CheckResult("citations", False, "citation_check_missing", {})
    for name in fields:
        if _residue(inp, name):
            return CheckResult(
                "citations", False, CITATION_UNRESOLVED, {"field": name, "why": "text_not_accounted_for"}
            )
    sentences = [s for s in inp.sentences if s.field in fields]
    for sentence in sentences:
        unknown_fact = [fid for fid in sentence.fact_ids if fid not in inp.facts]
        bad_claim = [
            i for i in sentence.claim_indexes if not 0 <= i < len(inp.claims) or not _has_source(inp.claims[i])
        ]
        if unknown_fact or bad_claim:
            return CheckResult(
                "citations",
                False,
                CITATION_UNRESOLVED,
                {"field": sentence.field, "unknown_facts": len(unknown_fact), "unknown_claims": len(bad_claim)},
            )
    verdicts = _citation_check(
        sentences, facts=inp.facts, claims=inp.claims, skills=inp.skills, job_terms=inp.job_terms, principal=principal
    )
    for sentence, reason in zip(sentences, verdicts, strict=True):
        if reason is not None:
            code = reason if reason in (CITATION_UNRESOLVED, UNSUPPORTED_CLAIM) else UNSUPPORTED_CLAIM
            return CheckResult("citations", False, code, {"field": sentence.field, "quote": sentence.text})
    return CheckResult("citations", True, None, {"sentences": len(sentences)})


def _has_source(claim: Mapping[str, Any]) -> bool:
    sources = claim.get("sources") or []
    return bool(sources) and bool(sources[0].get("url"))


def check_consent(inp: CheckInput) -> CheckResult:
    if inp.consent:
        return CheckResult("consent", True, None, {"purpose": "ai_processing"})
    return CheckResult("consent", False, "consent_missing", {"purpose": "ai_processing"})


def _phones(value: str) -> list[str]:
    low, high = _PHONE_DIGITS
    return [m.group() for m in _PHONE.finditer(value) if low <= sum(c.isdigit() for c in m.group()) <= high]


def _restricted(inp: CheckInput) -> list[str]:
    """Which kinds of restricted data the texts hold, by name."""
    found: list[str] = []
    joined = "\n".join(inp.texts.values())
    if _EMAIL.search(joined) or _phones(joined):
        found.append("contact_details")
    if _PAY.search(joined):
        found.append("pay")
    if inp.salary_floor:
        squeezed = joined.lower().replace(",", "").replace(" ", "")
        if str(inp.salary_floor) in squeezed or f"{inp.salary_floor // 1000}k" in squeezed:
            found.append("salary_floor")
    if _PROTECTED.search(joined):
        found.append("protected_characteristic")
    return found


def check_scope(inp: CheckInput) -> CheckResult:
    others = [kind for kind in inp.violations if kind != "excluded_company"]
    if others:
        return CheckResult("scope", False, "constraint_conflict", {"kinds": others})
    if "excluded_company" in inp.violations:
        return CheckResult("scope", False, "employer_blocked", {})
    if inp.action_type == "send_follow_up" and (
        not inp.recipient
        or not inp.gate.follow_up_email
        or inp.recipient.strip().lower() != inp.gate.follow_up_email.strip().lower()
    ):
        return CheckResult("scope", False, "restricted_data", {"what": "recipient"})
    restricted = _restricted(inp)
    if restricted:
        return CheckResult("scope", False, "restricted_data", {"what": restricted})
    return CheckResult("scope", True, None, {})


def check_budget(inp: CheckInput) -> CheckResult:
    if inp.campaign_exhausted or inp.month_exhausted:
        return CheckResult(
            "budget", False, "budget_exhausted", {"campaign": inp.campaign_exhausted, "month": inp.month_exhausted}
        )
    return CheckResult("budget", True, None, {})


def _outside_strings(value: str) -> list[str]:
    return (
        [m.group().rstrip(".,;:!?") for m in _URL.finditer(value)]
        + [m.group() for m in _EMAIL.finditer(value)]
        + _phones(value)
    )


def check_injection(inp: CheckInput) -> CheckResult:
    fields = _model_fields(inp)
    if not fields:
        return CheckResult("injection", True, None, {"applies": False})
    flagged = list(inp.input_flags)
    for sentence in inp.sentences:
        flagged += [
            "claim_flagged"
            for i in sentence.claim_indexes
            if 0 <= i < len(inp.claims) and inp.claims[i].get("injection_suspected")
        ]
    if flagged:
        return CheckResult("injection", False, "injection_suspected", {"flags": sorted(set(flagged))})
    known = " ".join(
        [
            *(str(f["statement"]) for f in inp.facts.values()),
            *(str(c.get("text", "")) for c in inp.claims),
            *(str(s.get("url", "")) for c in inp.claims for s in c.get("sources") or []),
            *inp.employer_urls,
            inp.gate.follow_up_email or "",
        ]
    ).lower()
    for name in fields:
        for item in _outside_strings(inp.texts[name]):
            if item.lower() not in known:
                return CheckResult(
                    "injection", False, "injection_suspected", {"field": name, "what": "outside_address"}
                )
    return CheckResult("injection", True, None, {})


def check_rate_limit(inp: CheckInput) -> CheckResult:
    if inp.action_type != "send_follow_up":
        return CheckResult("rate_limit", True, None, {"applies": False})
    if inp.follow_ups >= MAX_FOLLOW_UPS:
        return CheckResult("rate_limit", False, "limit_reached", {"limit": MAX_FOLLOW_UPS})
    return CheckResult("rate_limit", True, None, {})


def run(inp: CheckInput, *, principal: UUID | None) -> list[CheckResult]:
    """All six checks, in the order of CHECK_KINDS."""
    return [
        check_citations(inp, principal=principal),
        check_consent(inp),
        check_scope(inp),
        check_budget(inp),
        check_injection(inp),
        check_rate_limit(inp),
    ]


def failed(results: Sequence[CheckResult]) -> list[str]:
    """The reason codes of the failed checks, in order."""
    return [r.reason_code for r in results if not r.passed and r.reason_code]
