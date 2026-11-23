"""Record proposals from chat (CHT-03; AI_SYSTEM 4.8; plan 05-03): what the candidate says about themselves, offered
back as changes they can confirm. Nothing here changes the record.

`propose` runs the task record.propose over the candidate's current message only, so the evidence quote can come from
nothing but their own words: not an earlier message, a tool result or a posting. Each item the model returns is then
checked in code, and the ones that fail are dropped with a code in the log and never shown:

    the quote is an exact substring of the message         the field is on the allow-list
    nothing in it is a protected characteristic            the record does not already hold the same value
    the same change was not declined in the last 30 days   the record's own checks accept it

A kept item is stored by profile.api.propose_change as a `proposed` event with origin chat. The record, its version and
the person's constraints, preferences and facts are as they were until the candidate confirms it in the app.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from app.platform import db, log
from app.platform.errors import ApiError
from app.platform.llm import complete
from app.profile import api as profile

from .schemas import ProposalEvent, ProposalItem, ProposalList, ProposalValue, SalaryFloor

TASK = "record.propose"
PROMPT = (Path(__file__).parent / "prompts" / "record_propose.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)

DECLINE_MEMORY = timedelta(days=30)  # assumption: AI_SYSTEM 4.8, a change declined this recently is not offered again
MAX_PROPOSALS = 5  # assumption: items taken from one message; the schema cannot bound the list
MAX_QUOTE = 500  # the change log keeps the quote as the proposal's reason, and the record allows 500 characters
MAX_SHOWN = 200  # assumption: characters of a skill, certification or preference shown to the model

# What a proposal may touch (the allow-list): every constraint kind, every preference kind the record accepts, and the
# facts a person can state in a sentence. Experience goes through the resume. profile.service owns the same lists and
# a test pins these copies to them, because only profile.api may be imported from here.
CONSTRAINT_KINDS = (
    "location",
    "remote_policy",
    "work_authorization",
    "salary_floor",
    "employment_type",
    "seniority",
    "excluded_company",
)
PREFERENCE_KINDS = (
    "target_title",
    "target_skill",
    "industry",
    "company_size",
    "location",
    "remote_policy",
    "salary_target",
    "seniority",
    "untyped_dealbreaker",
    "other",
)
FACT_KINDS = ("skill", "certification")
FIELDS = {"constraint": CONSTRAINT_KINDS, "preference": PREFERENCE_KINDS, "fact": FACT_KINDS}
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "location": ("country_codes", "cities"),
    "work_authorization": ("country_codes",),
    "remote_policy": ("arrangements",),
    "employment_type": ("employment_types",),
    "seniority": ("seniorities",),
    "salary_floor": ("salary_floor",),
    "excluded_company": ("company_names",),
}
DEALBREAKER_WEIGHT = -3  # what the record gives an untyped dealbreaker, whatever is asked

# The record as get_record has it, cut to what a proposal may touch: constraints, preferences, skills, certifications.
Current = Mapping[str, Sequence[Mapping[str, Any]]]


# ---- what the model is shown ---------------------------------------------------------------------------------------


def _short(text: str) -> str:
    return " ".join(text.split())[:MAX_SHOWN]


def _describe(current: Current) -> str:
    """The values of the fields a proposal may touch, as they are now: what to start from when a value changes."""
    by_kind = {row["kind"]: row for row in current["constraints"]}
    lines = ["What the record holds now.", "Hard constraints:"]
    for kind in CONSTRAINT_KINDS:
        row = by_kind.get(kind)
        value = {n: row.get(n) for n in KIND_FIELDS[kind] if row is not None and row.get(n) is not None}
        lines.append(f"- {kind}: {json.dumps(value, ensure_ascii=False)}" if value else f"- {kind}: (not set)")
    lines.append("Preferences:")
    wanted = [f"- {p['kind']}: {_short(p['value'])} (weight {p['weight']})" for p in current["preferences"]]
    lines += wanted or ["- (none)"]
    for kind, label in (("skill", "Skills"), ("certification", "Certifications")):
        held = [_short(f["statement"]) for f in current["facts"] if f["kind"] == kind]
        lines.append(f"{label}: {'; '.join(held) if held else '(none)'}")
    return "\n".join(lines)


def build_messages(user_text: str, current: Current) -> list[dict[str, str]]:
    """The whole request: the prompt, the values the record holds now in the fields a proposal may touch, and the
    candidate's current message. Nothing else goes in: no earlier message, no tool result, no posting, no id."""
    user = f"{_describe(current)}\n\nBEGIN MESSAGE\n{user_text}\nEND MESSAGE"
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}]


# ---- checking what the model returned ------------------------------------------------------------------------------


def _text(value: object) -> str:
    return " ".join(str(value).casefold().split())


def _canon(value: Any) -> Any:
    """What a typed value says, whatever its order, case or spacing. An empty list says nothing, like None.

    ponytail: company names compare as typed, not by company key, so "Initech" and "Initech Inc" count as two different
    proposals; compare by match.checker.company_key once match.api exports it, if a duplicate card is ever reported."""
    if value is None or value == []:
        return None
    if isinstance(value, Mapping):
        return tuple(sorted((key, _canon(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return frozenset(_canon(item) for item in value)
    return _text(value) if isinstance(value, str) else value


def _key(entity: str, operation: str, row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Two changes with the same key say the same thing. `row` is what the change writes (or, for a removal, the row it
    removes), with its kind. A removal of a constraint is named by its kind alone, of a preference by its words."""
    kind, removal = row["kind"], operation == "remove"
    if entity == "constraint":
        said = () if removal else tuple(_canon(row.get(name)) for name in KIND_FIELDS[kind])
    elif entity == "preference":  # a kind with no vocabulary keeps the words themselves as its value
        said = (_text(row.get("value") or row.get("statement", "")), *(() if removal else (row.get("weight"),)))
    else:
        said = (_text(row.get("statement", "")),)
    return (entity, "remove" if removal else "set", kind, *said)


def _declined_keys(declined: Sequence[profile.DeclinedProposal]) -> set[tuple[Any, ...]]:
    return {_key(d.entity, d.operation, {**d.value, "kind": d.field}) for d in declined}


def _tidy(name: str, item: str) -> str:
    """The case the record requires, so that "in" is not refused for being a country code in lower case."""
    item = item.strip()
    if name == "country_codes":
        return item.upper()
    return item.lower() if name in ("arrangements", "employment_types", "seniorities") else item


def _typed(value: ProposalValue, kind: str) -> dict[str, Any]:
    """The typed fields of a constraint kind that a proposal fills in (an empty list is not filled in)."""
    typed: dict[str, Any] = {}
    for name in KIND_FIELDS[kind]:
        given = getattr(value, name)
        if isinstance(given, SalaryFloor):
            typed[name] = {"amount": given.amount, "currency": given.currency.strip().upper()}
        elif given:
            typed[name] = [_tidy(name, item) for item in given]
    return typed


def _words(item: ProposalItem) -> list[str]:
    """Every text of a proposal that would be stored or shown: the narrow screen looks at each (the record's own wider
    one looks again when the proposal is written)."""
    value = item.value
    return [item.statement, item.evidence_quote, value.text or "", *(value.cities or []), *(value.company_names or [])]


@dataclass(frozen=True)
class _Plan:
    entity: str
    operation: str  # create | update | remove, the record's words
    entity_id: UUID | None
    before: dict[str, Any] | None  # the row it changes
    after: dict[str, Any] | None  # what it writes
    statement: str
    quote: str
    key: tuple[Any, ...]  # what it says: equal keys are the same change


def _plan(item: ProposalItem, row: Mapping[str, Any] | None, after: dict[str, Any], same: bool) -> _Plan | str:
    """Create, update or remove, from the row the record holds now (None when it holds none) and whether that row
    already says what `after` says. A string is the reason to drop the proposal."""
    shown = {name: value for name, value in row.items() if name not in ("created_at", "notice")} if row else None
    target = UUID(str(row["id"])) if row is not None else None
    if item.operation == "remove":
        if row is None:
            return "nothing_to_remove"
        key = _key(item.entity, "remove", row)
        return _Plan(item.entity, "remove", target, shown, None, item.statement, item.evidence_quote, key)
    if same:
        return "unchanged"
    operation = "update" if row is not None else "create"
    key = _key(item.entity, operation, after)
    return _Plan(item.entity, operation, target, shown, after, item.statement, item.evidence_quote, key)


def _check(item: ProposalItem, user_text: str, current: Current, declined: set[tuple[Any, ...]]) -> _Plan | str:
    """One proposal against the rules of AI_SYSTEM 4.8. Returns the plan, or the code of the rule it broke."""
    if not item.evidence_quote.strip() or item.evidence_quote not in user_text:
        return "quote_not_in_message"  # job text, tool output and earlier messages can never originate a proposal
    if item.field not in FIELDS[item.entity] or (item.entity == "fact" and item.operation == "remove"):
        return "not_allowed"
    if any(profile.screen(text) for text in _words(item)):
        return "protected_characteristic"
    value, kind = item.value, item.field
    row: Mapping[str, Any] | None
    if item.entity == "constraint":
        row = {c["kind"]: c for c in current["constraints"]}.get(kind)
        after: dict[str, Any] = {"kind": kind, "statement": item.statement, **_typed(value, kind)}
        same = row is not None and _key("constraint", "set", after) == _key("constraint", "set", row)
    elif item.entity == "preference":
        weight = DEALBREAKER_WEIGHT if kind == "untyped_dealbreaker" else value.weight
        words = (value.text or "").strip() or None
        after = {"kind": kind, "statement": item.statement, "value": words, "weight": weight}
        said = _text(words or item.statement)  # a kind with no vocabulary keeps the words themselves as its value
        row = next((p for p in current["preferences"] if p["kind"] == kind and _text(p["value"]) == said), None)
        same = row is not None and row["weight"] == weight
    else:  # a skill or certification: set means add, and one the record holds already is dropped
        name = (value.text or "").strip()
        if not name:
            return "no_value"
        row, after = None, {"kind": kind, "statement": name}
        same = any(f["kind"] == kind and _text(f["statement"]) == _text(name) for f in current["facts"])
    planned = _plan(item, row, after, same)
    if isinstance(planned, str):
        return planned
    return "declined_before" if planned.key in declined else planned


# ---- the proposals -------------------------------------------------------------------------------------------------


def propose(user_id: UUID, user_text: str, *, principal: UUID | None) -> list[ProposalEvent]:
    """The record changes that the candidate's message states, stored as proposals and returned as the events the stream
    sends. `user_id` is the signed-in person's and `principal` is who the model call is attributed to (the same
    person). The gateway's AIDisabled, ProviderUnavailable and SchemaInvalid are not caught: the caller maps them.

    No connection is held while the model answers: the record is read first and the proposals are written after."""
    since = datetime.now(timezone.utc) - DECLINE_MEMORY
    with db.transaction(user_id) as conn:
        record = profile.get_record(conn, user_id)
        declined = _declined_keys(profile.declined_since(conn, user_id, since))
    current = {
        "constraints": record["constraints"],
        "preferences": record["preferences"],
        "facts": [fact for fact in record["facts"] if fact["kind"] in FACT_KINDS],
    }
    messages = build_messages(user_text, current)
    answer = cast(
        ProposalList,
        complete(TASK, ProposalList, messages, principal=principal, prompt_version=PROMPT_VERSION, ledger="api"),
    )

    plans: list[_Plan] = []
    for item in answer.proposals[:MAX_PROPOSALS]:
        planned = _check(item, user_text, current, declined)
        if not isinstance(planned, str) and any(planned.key == kept.key for kept in plans):
            planned = "duplicate"
        if isinstance(planned, str):
            log.event("chat.proposal_dropped", error_code=planned)  # a code, never the words
        else:
            plans.append(planned)
    if not plans:
        return []
    events: list[ProposalEvent] = []
    with db.transaction(user_id) as conn:
        for plan in plans:
            try:
                event_id = profile.propose_change(
                    conn,
                    user_id,
                    origin="chat",
                    entity=plan.entity,
                    operation=plan.operation,
                    after=plan.after,
                    entity_id=plan.entity_id,
                    reason=plan.quote[:MAX_QUOTE],
                )
            except ApiError as refused:
                # the record's own checks refuse it: a wrong type, a protected characteristic, a row that changed
                log.event("chat.proposal_dropped", error_code=refused.code)
                continue
            events.append(ProposalEvent(event_id, plan.entity, plan.operation, plan.before, plan.after, plan.statement))
    return events
