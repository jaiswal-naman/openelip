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

GUARDIAN_VERSION = "guardian-2026-11-b"
CHECK_KINDS = ("citations", "consent", "scope", "budget", "injection", "rate_limit")
MAX_FOLLOW_UPS = 2  # per campaign (N11)
SESSION_QUESTIONS = 40  # assumption, AI_SYSTEM 8: questions one Twin session takes (twin.api.SESSION_QUESTIONS; a test
# keeps the two equal, since the Guardian may not import the Twin)
SESSION_COST_MICROUSD = 500_000  # assumption, AI_SYSTEM 8: $0.50 of model cost for one Twin session
# INDEX decision 30: which topic each kind of confirmed fact belongs to (twin.logic.FACT_TOPIC; a test keeps them equal)
TWIN_FACT_TOPIC = {
    "role": "experience",
    "achievement": "experience",
    "skill": "skills",
    "certification": "skills",
    "education": "education",
    "project": "projects",
    "other": "other",
}
OPEN_PERMISSIONS = ("answer", "ask_me")
DEFAULT_PERMISSION = "ask_me"  # AI_SYSTEM 8.2: no row means ask me; pay and contact are never open
LOCKED_TOPICS = ("current_pay", "contact_details")
TWIN_TOPICS = frozenset(
    {
        "experience", "skills", "education", "projects", "availability", "work_authorization", "location",
        "expected_pay", "motivation", "current_pay", "contact_details", "other",
    }
)  # fmt: skip

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
    twin: "TwinInput | None" = None  # a twin_answer's own inputs (the fields above that do not apply stay empty)


@dataclass(frozen=True)
class TwinInput:
    """What the checks of a `twin_answer` read: the payload's parts, the candidate's confirmed facts it cites, which
    topics are open, whether the introduction consent still stands and how much of the session is used."""

    kind: str  # answer | relay
    topic: str
    permission: str  # the candidate's permission for the topic now
    body: str
    sentences: tuple[tuple[str, tuple[str, ...]], ...]  # (text, cited fact ids)
    cited_fact_ids: tuple[str, ...]
    facts: Mapping[str, dict[str, Any]]  # the cited facts that are confirmed and usable, by id
    open_topics: frozenset[str]
    consistent: bool  # the payload names this session, its organization and its introduction
    consent: bool
    questions: int
    spent_microusd: int


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
    if action_type == "twin_answer":
        return gather_twin(conn, payload, provenance, gate)
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
    stated = (constraints.get("salary_floor") or {}).get("salary_floor")
    floor = stated.get("amount") if isinstance(stated, dict) else stated  # compiled as {"amount", "currency"}
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


def topic_permissions(conn: Connection, user_id: UUID) -> dict[str, str]:
    """The candidate's permission for every topic: their rows, `ask_me` for a topic with none, `never` for pay and
    contact details whatever a row says."""
    found = {
        str(r.topic): str(r.permission)
        for r in conn.execute(
            text("SELECT topic::text AS topic, permission::text AS permission FROM twin_scopes WHERE user_id = :u"),
            {"u": user_id},
        )
    }
    return {
        **{topic: found.get(topic, DEFAULT_PERMISSION) for topic in sorted(TWIN_TOPICS)},
        **dict.fromkeys(LOCKED_TOPICS, "never"),
    }


def topic_permission(conn: Connection, user_id: UUID, topic: object) -> str:
    """The permission for one topic; a topic that is not one of the twelve counts as never."""
    permissions = topic_permissions(conn, user_id)
    return permissions.get(str(topic), "never") if str(topic) in TWIN_TOPICS else "never"


def gather_twin(
    conn: Connection, payload: Mapping[str, Any], provenance: Mapping[str, Any], gate: SubjectGate
) -> CheckInput:
    """Read what the six checks need for a `twin_answer` (the gate is the session). Nothing here calls a model."""
    user_id = gate.user_id
    kind = str(payload.get("kind"))
    topic = str(payload.get("topic"))
    sentences = tuple(
        (str(item.get("text") or ""), tuple(str(x) for x in item.get("fact_ids", ())))
        for item in payload.get("sentences") or []
    )
    cited = tuple(str(x) for x in payload.get("cited_fact_ids") or ())
    wanted = {fid for _, ids in sentences for fid in ids} | set(cited)
    usable = {str(row["id"]): row for row in profile.usable_facts(conn, user_id) if str(row["id"]) in wanted}
    permissions = topic_permissions(conn, user_id)
    session = conn.execute(
        text(
            """SELECT s.introduction_id, s.organization_id,
                      has_active_consent(i.user_id, 'introduction', i.organization_id, i.role_id) AS consent,
                      (SELECT count(*) FROM twin_messages m WHERE m.session_id = s.id AND m.author = 'employer')
                        AS questions,
                      COALESCE((SELECT sum(l.cost_microusd) FROM llm_calls l JOIN agent_runs r ON r.id = l.agent_run_id
                                WHERE r.twin_session_id = s.id), 0) AS spent
                 FROM twin_sessions s JOIN introductions i ON i.id = s.introduction_id
                WHERE s.id = :s AND s.user_id = :u"""
        ),
        {"s": gate.id, "u": user_id},
    ).one_or_none()
    consistent = (
        session is not None
        and str(payload.get("session_id")) == str(gate.id)
        and str(payload.get("introduction_id")) == str(session.introduction_id)
        and str(payload.get("organization_id")) == str(session.organization_id)
    )
    body = str(payload.get("body") or "")
    return CheckInput(
        action_type="twin_answer",
        gate=gate,
        texts={"body": body},
        sources={"body": "candidate" if kind == "relay" else "model"},
        recipient=None,
        input_flags=tuple(str(x) for x in provenance.get("input_flags") or ()),
        twin=TwinInput(
            kind=kind,
            topic=topic,
            permission=permissions.get(topic, "never"),
            body=body,
            sentences=sentences,
            cited_fact_ids=cited,
            facts=usable,
            open_topics=frozenset(t for t, p in permissions.items() if p in OPEN_PERMISSIONS),
            consistent=consistent,
            consent=bool(session.consent) if session is not None else False,
            questions=int(session.questions) if session is not None else 0,
            spent_microusd=int(session.spent) if session is not None else 0,
        ),
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


_DATE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{4}")


def _phones(value: str) -> list[str]:
    """Digit runs that look like a phone number; a date written with digits is not one."""
    low, high = _PHONE_DIGITS
    found = [m.group() for m in _PHONE.finditer(value)]
    return [p for p in found if low <= sum(c.isdigit() for c in p) <= high and not _DATE.fullmatch(p.strip(" .-"))]


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


def _twin_citations(tw: TwinInput) -> CheckResult:
    """A drafted answer cites only confirmed facts of this candidate whose topic is open, in every sentence, and says
    nothing outside its sentences. A relay is the candidate's own words: the check does not apply."""
    if tw.kind == "relay":
        return CheckResult("citations", True, None, {"applies": False})
    if not tw.sentences or not tw.body.strip():
        return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "no_sentences"})
    left = tw.body
    for sentence, _ in tw.sentences:
        if sentence not in left:
            return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "text_not_accounted_for"})
        left = left.replace(sentence, "", 1)
    if left.strip(" \n\t\r.,;:!?-—–"):
        return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "text_not_accounted_for"})
    seen: set[str] = set()
    for sentence, ids in tw.sentences:
        if not ids:
            return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "uncited_sentence"})
        for fact_id in ids:
            fact = tw.facts.get(fact_id)
            if fact is None:
                return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "fact_not_confirmed"})
            if TWIN_FACT_TOPIC.get(str(fact["kind"])) not in tw.open_topics:
                return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "topic_closed"})
            seen.add(fact_id)
    if seen != set(tw.cited_fact_ids):
        return CheckResult("citations", False, CITATION_UNRESOLVED, {"why": "cited_ids_differ"})
    return CheckResult("citations", True, None, {"sentences": len(tw.sentences)})


def _twin_scope(tw: TwinInput) -> CheckResult:
    """Nothing about pay or contact details reaches the employer: not in the body, any sentence or the statement of any
    cited fact (the employer reads those as quotes), and nothing on a topic the candidate closed."""
    if not tw.consistent:
        return CheckResult("scope", False, "restricted_data", {"what": "recipient"})
    if tw.permission == "never":
        return CheckResult("scope", False, "topic_not_open", {"topic": tw.topic})
    found = {m.kind for m in trust.restricted_matches(tw.body)}
    for sentence, _ in tw.sentences:
        found |= {m.kind for m in trust.restricted_matches(sentence)}
    if found:
        return CheckResult("scope", False, "restricted_data", {"what": sorted(found)})
    for fact_id, fact in tw.facts.items():
        if trust.restricted_matches(str(fact["statement"])):
            return CheckResult("scope", False, "restricted_data", {"what": "cited_fact", "fact_id": fact_id})
    return CheckResult("scope", True, None, {})


def _twin_injection(inp: CheckInput, tw: TwinInput) -> CheckResult:
    """A drafted answer holds no address or link that the cited facts do not hold. Does not apply to a relay."""
    if tw.kind == "relay":
        return CheckResult("injection", True, None, {"applies": False})
    if inp.input_flags:
        return CheckResult("injection", False, "injection_suspected", {"flags": sorted(set(inp.input_flags))})
    known = " ".join(str(f["statement"]) for f in tw.facts.values()).lower()
    for item in _outside_strings(tw.body):
        if item.lower() not in known:
            return CheckResult("injection", False, "injection_suspected", {"what": "outside_address"})
    return CheckResult("injection", True, None, {})


def _twin_rate_limit(tw: TwinInput) -> CheckResult:
    if tw.questions > SESSION_QUESTIONS:
        return CheckResult("rate_limit", False, "limit_reached", {"limit": "questions", "of": SESSION_QUESTIONS})
    if tw.spent_microusd >= SESSION_COST_MICROUSD:
        return CheckResult("rate_limit", False, "limit_reached", {"limit": "cost", "of": SESSION_COST_MICROUSD})
    return CheckResult("rate_limit", True, None, {})


def run_twin(inp: CheckInput) -> list[CheckResult]:
    """The six checks of a `twin_answer`, in the order of CHECK_KINDS. It has no model call: the model budget does not
    apply (the session's own cost cap does, in rate_limit)."""
    tw = inp.twin
    assert tw is not None
    return [
        _twin_citations(tw),
        CheckResult("consent", tw.consent, None if tw.consent else "consent_missing", {"purpose": "introduction"}),
        _twin_scope(tw),
        CheckResult("budget", True, None, {"applies": False}),
        _twin_injection(inp, tw),
        _twin_rate_limit(tw),
    ]


def run(inp: CheckInput, *, principal: UUID | None) -> list[CheckResult]:
    """All six checks, in the order of CHECK_KINDS."""
    if inp.twin is not None:
        return run_twin(inp)
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
