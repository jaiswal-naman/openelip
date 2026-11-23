"""Operations on the Career Record: facts, hard constraints and soft preferences, and the reads behind the
candidate's own pages (REC-02 to REC-05 and REC-07; ARCHITECTURE 4.2). Proposals and the change-log read are in
app.profile.events.

Rules this module keeps:
  * Rows are never edited. A change inserts a new row and fills the write-once lifecycle columns of the old one.
  * Every change is ONE profile_events row and ONE bump of users.record_version, written by _apply in the caller's
    transaction (inside a savepoint, so a failed operation leaves neither). There is no other write path and no event
    is published: the version and the row are the change signal, and nothing consumes record changes before Phase 4.
  * Text a candidate wrote is screened for protected characteristics before anything is written (REC-07).
  * Operations on one person's record run one at a time (a row lock on users), so the checks they make still hold when
    they are applied.

Other modules never import this file: they use app.profile.api.
"""

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from typing import Any, Concatenate, ParamSpec, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import Connection, Row, func, insert, text, update

from . import intake, models, protected
from .errors import (
    BadCursor,
    ConstraintNotEnforceable,
    InvalidField,
    ProtectedCharacteristic,
    RecordNotFound,
    StateConflict,
)

MAX_STATEMENT = 2000  # the facts table's own limit; the other statements follow it
MAX_REASON = 500
MAX_VALUE = 200  # a preference's normalized token
MAX_NAME = 200  # one company or one city
MAX_ITEMS = 50  # entries in one list field
MAX_INT4 = 2_147_483_647
RECENT_DOCUMENTS = 20
DEALBREAKER_WEIGHT = -3
NOT_GUARANTEED = "not_guaranteed"  # the notice on a dealbreaker that no job field can check (REC-04)

# What each kind of hard constraint holds. The first field is the one a kind cannot do without.
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "location": ("country_codes", "cities"),
    "work_authorization": ("country_codes",),
    "remote_policy": ("arrangements",),
    "employment_type": ("employment_types",),
    "seniority": ("seniorities",),
    "salary_floor": ("salary_floor",),
    "excluded_company": ("company_names",),
}
TYPED_FIELDS = tuple(dict.fromkeys(name for names in KIND_FIELDS.values() for name in names))
_ENUM_FIELDS = {
    "arrangements": models.WORK_ARRANGEMENTS,
    "employment_types": models.EMPLOYMENT_TYPES,
    "seniorities": models.SENIORITIES,
}

# Fact status is derived from the lifecycle timestamps (API.md 1.12). USABLE_FACT is the predicate of the partial index
# profile_facts_usable_idx: confirmed, not rejected, not superseded (REC-02). Every read for another module uses it.
USABLE_FACT = "f.confirmed_at IS NOT NULL AND f.rejected_at IS NULL AND f.superseded_at IS NULL"
FACT_STATUS = """CASE WHEN f.superseded_at IS NOT NULL THEN 'superseded' WHEN f.rejected_at IS NOT NULL THEN 'rejected'
            WHEN f.confirmed_at IS NOT NULL THEN 'confirmed' ELSE 'unconfirmed' END"""
FACT_COLUMNS = f"""f.id, f.kind, f.statement, f.attributes, f.source, f.parent_fact_id, f.created_at, f.document_id,
       lower(f.source_span) AS span_start, upper(f.source_span) AS span_end, f.source_quote, {FACT_STATUS} AS status"""
FACT_ORDER = "f.created_at, COALESCE(lower(f.source_span), 2147483647), f.id"  # document order within one parse
_FACT_STATUS_WHERE = {
    "unconfirmed": "f.confirmed_at IS NULL AND f.rejected_at IS NULL AND f.superseded_at IS NULL",
    "confirmed": USABLE_FACT,
    "rejected": "f.rejected_at IS NOT NULL AND f.superseded_at IS NULL",
    "superseded": "f.superseded_at IS NOT NULL",
}
# Enum arrays are read as text[]: the driver returns an array of a custom enum as one string otherwise.
CONSTRAINT_COLUMNS = """c.id, c.kind, c.statement, c.source, c.created_at, c.country_codes::text[] AS country_codes,
       c.cities, c.arrangements::text[] AS arrangements, c.employment_types::text[] AS employment_types,
       c.seniorities::text[] AS seniorities, c.salary_floor, c.salary_currency, c.company_names"""
PREFERENCE_COLUMNS = "p.id, p.kind, p.value, p.weight, p.statement, p.source, p.created_at"

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True)
class Via:
    """Who made a change: the candidate directly, or by confirming a proposal (then the proposal's id and reason)."""

    source: str = "candidate_edit"
    proposal_event_id: UUID | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.source not in ("candidate_edit", "confirmed_proposal"):
            raise ValueError("a record row written here is never sourced from a resume")
        if (self.source == "confirmed_proposal") != (self.proposal_event_id is not None):
            raise ValueError("a confirmed proposal names its proposal, and only a confirmed proposal does")


DIRECT = Via()


@dataclass(frozen=True)
class Applied:
    """What a change produced: the kind of row (fact, constraint or preference), the row after the change as the API
    shows it, the change-log row's id and the version of the record after it."""

    entity: str
    row: dict[str, Any]
    event_id: UUID
    record_version: int


def atomic(operation: Callable[Concatenate[Connection, P], R]) -> Callable[Concatenate[Connection, P], R]:
    """Run an operation in a savepoint: if it fails, neither its event nor its version bump nor its rows remain."""

    @wraps(operation)
    def run(conn: Connection, *args: P.args, **kwargs: P.kwargs) -> R:
        with conn.begin_nested():
            return operation(conn, *args, **kwargs)

    return run


# ---- checking what a candidate sends -------------------------------------------------------------


def clean_text(value: object, field: str, max_length: int = MAX_STATEMENT) -> str:
    """Text a candidate wrote, trimmed and checked. A protected characteristic is refused here (REC-07), so nothing
    that states one reaches any table. This is typed text, so it goes through the wide screen: plain sentences ("I am
    45 years old") as well as labels. What a resume line says is checked by the narrow one (extract/validate.py)."""
    if not isinstance(value, str):
        raise InvalidField(field, "invalid_value")
    cleaned = value.strip()
    if "\x00" in cleaned:  # PostgreSQL text cannot hold it
        raise InvalidField(field, "invalid_characters")
    if not cleaned:
        raise InvalidField(field, "required")
    if len(cleaned) > max_length:
        raise InvalidField(field, "too_long")
    if (category := protected.screen_typed(cleaned)) is not None:
        raise ProtectedCharacteristic(category, field)
    return cleaned


def optional_reason(value: object) -> str | None:
    return None if value is None else clean_text(value, "reason", MAX_REASON)


def one_of(value: object, allowed: Sequence[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise InvalidField(field, "invalid_value")
    return value


def only_keys(given: Mapping[str, Any], allowed: Sequence[str]) -> None:
    for name in given:
        if name not in allowed:
            raise InvalidField(str(name), "unknown_field")


def _dedupe(items: list[str], *, ignore_case: bool) -> list[str]:
    seen: set[str] = set()
    kept = []
    for item in items:
        key = item.casefold() if ignore_case else item
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def _clean_salary(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidField("salary_floor", "invalid_value")
    amount, currency = value.get("amount"), value.get("currency")
    if not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= MAX_INT4:
        raise InvalidField("salary_floor.amount", "invalid_value")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise InvalidField("salary_floor.currency", "invalid_value")
    return {"amount": amount, "currency": currency}


def _clean_field(name: str, value: object) -> Any:
    """One typed field of a hard constraint: a list of allowed values, never guessed or repaired."""
    if name == "salary_floor":
        return _clean_salary(value)
    if not isinstance(value, list | tuple) or not 0 < len(value) <= MAX_ITEMS:
        raise InvalidField(name, "invalid_value")
    if name in _ENUM_FIELDS:
        return _dedupe([one_of(item, _ENUM_FIELDS[name], name) for item in value], ignore_case=False)
    if name == "country_codes":
        if not all(isinstance(item, str) and re.fullmatch(r"[A-Z]{2}", item) for item in value):
            raise InvalidField(name, "invalid_value")
        return _dedupe(list(value), ignore_case=False)
    if name == "cities":  # lower case canonical names (DATA_MODEL 3.4)
        return _dedupe([" ".join(clean_text(i, name, MAX_NAME).lower().split()) for i in value], ignore_case=False)
    return _dedupe([clean_text(item, name, MAX_NAME) for item in value], ignore_case=True)  # company_names, as typed


def clean_constraint(kind: object, fields: Mapping[str, Any], statement: object) -> dict[str, Any]:
    """A hard constraint as the API shows it. The statement is checked first, so a protected characteristic is refused
    before the kind is looked at and is never echoed back as a suggestion."""
    sentence = clean_text(statement, "statement")
    if not isinstance(kind, str) or kind not in models.CONSTRAINT_KINDS:
        raise ConstraintNotEnforceable(sentence)
    allowed = KIND_FIELDS[kind]
    given = {name: value for name, value in fields.items() if value is not None}
    for name in given:
        if name not in allowed:
            raise InvalidField(name, "not_used_by_kind" if name in TYPED_FIELDS else "unknown_field")
    clean: dict[str, Any] = {"kind": kind, "statement": sentence, **dict.fromkeys(TYPED_FIELDS)}
    for name, value in given.items():
        clean[name] = _clean_field(name, value)
    if clean[allowed[0]] is None:
        raise InvalidField(allowed[0], "required")
    return clean


def clean_preference(kind: object, statement: object, value: object, weight: object) -> dict[str, Any]:
    sentence = clean_text(statement, "statement")
    kind = one_of(kind, models.PREFERENCE_KINDS, "kind")
    if value is not None:
        token = clean_text(value, "value", MAX_VALUE)
    elif kind in ("untyped_dealbreaker", "other"):  # kinds with no vocabulary: the words are the value
        token = " ".join(sentence.lower().split())
    else:
        raise InvalidField("value", "required")
    if kind == "untyped_dealbreaker":
        if weight not in (None, DEALBREAKER_WEIGHT):
            raise InvalidField("weight", "invalid_value")
        weight = DEALBREAKER_WEIGHT
    elif weight is None:
        raise InvalidField("weight", "required")
    elif not isinstance(weight, int) or isinstance(weight, bool) or weight == 0 or not -3 <= weight <= 3:
        raise InvalidField("weight", "invalid_value")
    return {"kind": kind, "value": token, "weight": weight, "statement": sentence}


# ---- rows as the API shows them ------------------------------------------------------------------


def _text_id(value: UUID | None) -> str | None:
    return None if value is None else str(value)


def fact_snapshot(row: Row[Any]) -> dict[str, Any]:
    """A fact as stored in the change log and shown by the API (JSON-ready; view() adds created_at)."""
    provenance = None
    if row.document_id is not None:
        span = {"start": row.span_start, "end": row.span_end}
        provenance = {"document_id": str(row.document_id), "span": span, "quote": row.source_quote}
    return {
        "id": str(row.id),
        "kind": row.kind,
        "status": row.status,
        "statement": row.statement,
        "attributes": row.attributes,
        "source": row.source,
        "parent_fact_id": _text_id(row.parent_fact_id),
        "provenance": provenance,
    }


def constraint_snapshot(row: Row[Any]) -> dict[str, Any]:
    floor = None if row.salary_floor is None else {"amount": row.salary_floor, "currency": row.salary_currency}
    return {
        "id": str(row.id),
        "kind": row.kind,
        "statement": row.statement,
        "source": row.source,
        "country_codes": row.country_codes,
        "cities": row.cities,
        "arrangements": row.arrangements,
        "employment_types": row.employment_types,
        "seniorities": row.seniorities,
        "salary_floor": floor,
        "company_names": row.company_names,
    }


def preference_snapshot(row: Row[Any]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "kind": row.kind,
        "value": row.value,
        "weight": row.weight,
        "statement": row.statement,
        "source": row.source,
    }


def view(snapshot: dict[str, Any], created_at: datetime) -> dict[str, Any]:
    """A fact or a constraint as the API shows it: its snapshot and when the row was made."""
    return {**snapshot, "created_at": created_at}


def preference_view(snapshot: dict[str, Any], created_at: datetime) -> dict[str, Any]:
    """A preference as the API shows it. A dealbreaker no job field can check says so (REC-04)."""
    notice = NOT_GUARANTEED if snapshot["kind"] == "untyped_dealbreaker" else None
    return {**snapshot, "created_at": created_at, "notice": notice}


def suggested_constraint(statement: str) -> dict[str, Any] | None:
    """The hard constraint a dealbreaker could be typed as, in the shape POST /v1/me/constraints takes, or None. Only
    a suggestion: nothing becomes a hard constraint until the candidate confirms one."""
    suggestion = intake.type_dealbreaker(statement)
    if suggestion is None:
        return None
    fields = dict(suggestion.fields)
    if suggestion.kind == "salary_floor":
        fields = {"salary_floor": {"amount": fields["salary_floor"], "currency": fields["salary_currency"]}}
    try:
        return clean_constraint(suggestion.kind, fields, statement)
    except (InvalidField, ConstraintNotEnforceable):  # a suggestion the constraint rules would refuse is no offer
        return None


# ---- locking, loading and the one write path -----------------------------------------------------


def lock_record(conn: Connection, user_id: UUID) -> None:
    """Serialize operations on this person's record. NO KEY UPDATE conflicts with itself and not with the key-share
    locks that foreign keys take, so sign-in and idempotency rows are not held up."""
    if conn.execute(text("SELECT 1 FROM users WHERE id = :u FOR NO KEY UPDATE"), {"u": user_id}).first() is None:
        raise RecordNotFound()


def fact_row(conn: Connection, user_id: UUID, fact_id: UUID) -> Row[Any]:
    sql = f"SELECT {FACT_COLUMNS} FROM profile_facts f WHERE f.id = :id AND f.user_id = :u"
    row = conn.execute(text(sql), {"id": fact_id, "u": user_id}).first()
    if row is None:
        raise RecordNotFound()
    return row


def active_constraint(conn: Connection, user_id: UUID, constraint_id: UUID) -> Row[Any]:
    sql = f"""SELECT {CONSTRAINT_COLUMNS} FROM constraints c
              WHERE c.id = :id AND c.user_id = :u AND c.superseded_at IS NULL"""
    row = conn.execute(text(sql), {"id": constraint_id, "u": user_id}).first()
    if row is None:
        raise RecordNotFound()
    return row


def active_preference(conn: Connection, user_id: UUID, preference_id: UUID) -> Row[Any]:
    sql = f"""SELECT {PREFERENCE_COLUMNS} FROM preferences p
              WHERE p.id = :id AND p.user_id = :u AND p.superseded_at IS NULL"""
    row = conn.execute(text(sql), {"id": preference_id, "u": user_id}).first()
    if row is None:
        raise RecordNotFound()
    return row


def _apply(
    conn: Connection,
    user_id: UUID,
    via: Via,
    entity: str,
    operation: str,
    entity_id: UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    reason: str | None = None,
    confirms_fact: bool = False,
) -> tuple[UUID, int]:
    """The one write path: bump the version, then write the change-log row that says what changed. Every operation
    calls this inside its savepoint and then writes the rows the event describes (a new row points back at the event).

    The first fact the person confirms sets record_confirmed_at. The rows written here take clock_timestamp() for
    created_at (as does the event), which keeps rows of one transaction in the order they were made: now() would give
    them all one time.
    """
    version = conn.execute(
        text(
            """UPDATE users SET record_version = record_version + 1,
                      record_confirmed_at = CASE WHEN :confirms THEN COALESCE(record_confirmed_at, now())
                                                 ELSE record_confirmed_at END
               WHERE id = :u RETURNING record_version"""
        ),
        {"u": user_id, "confirms": confirms_fact},
    ).scalar_one()
    event_id: UUID = conn.execute(
        insert(models.profile_events)
        .values(
            user_id=user_id,
            event_type="applied",
            entity=entity,
            operation=operation,
            entity_id=entity_id,
            before=before,
            after=after,
            source=via.source,
            proposal_event_id=via.proposal_event_id,
            reason=reason if reason is not None else via.reason,
            record_version_after=version,
            created_at=func.clock_timestamp(),
        )
        .returning(models.profile_events.c.id)
    ).scalar_one()
    return event_id, int(version)


# ---- facts ---------------------------------------------------------------------------------------


def _written_fact(fact_id: UUID, kind: str, statement: str, parent: str | None, via: Via) -> dict[str, Any]:
    """A fact the candidate wrote: confirmed on creation, with no span because it did not come from a document."""
    return {
        "id": str(fact_id),
        "kind": kind,
        "status": "confirmed",
        "statement": statement,
        "attributes": {},
        "source": via.source,
        "parent_fact_id": parent,
        "provenance": None,
    }


def _insert_fact(conn: Connection, user_id: UUID, snapshot: dict[str, Any], event_id: UUID) -> datetime:
    created_at: datetime = conn.execute(
        insert(models.profile_facts)
        .values(
            id=UUID(snapshot["id"]),
            user_id=user_id,
            kind=snapshot["kind"],
            statement=snapshot["statement"],
            parent_fact_id=snapshot["parent_fact_id"],
            source=snapshot["source"],
            profile_event_id=event_id,
            created_at=func.clock_timestamp(),
            confirmed_at=func.now(),
        )
        .returning(models.profile_facts.c.created_at)
    ).scalar_one()
    return created_at


@atomic
def add_fact(conn: Connection, user_id: UUID, kind: str, statement: str, *, via: Via = DIRECT) -> Applied:
    """A fact the candidate states by hand. It is confirmed on creation: they wrote it."""
    kind = one_of(kind, models.FACT_KINDS, "kind")
    statement = clean_text(statement, "statement")
    lock_record(conn, user_id)
    new = _written_fact(uuid4(), kind, statement, None, via)
    event_id, version = _apply(conn, user_id, via, "fact", "create", UUID(new["id"]), None, new, confirms_fact=True)
    return Applied("fact", view(new, _insert_fact(conn, user_id, new, event_id)), event_id, version)


@atomic
def confirm_fact(conn: Connection, user_id: UUID, fact_id: UUID) -> Applied:
    lock_record(conn, user_id)
    fact = fact_row(conn, user_id, fact_id)
    if fact.status != "unconfirmed":
        raise StateConflict(fact.status)
    before = fact_snapshot(fact)
    after = {**before, "status": "confirmed"}
    event_id, version = _apply(conn, user_id, DIRECT, "fact", "confirm", fact_id, before, after, confirms_fact=True)
    conn.execute(
        update(models.profile_facts).where(models.profile_facts.c.id == fact_id).values(confirmed_at=func.now())
    )
    return Applied("fact", view(after, fact.created_at), event_id, version)


@atomic
def correct_fact(
    conn: Connection, user_id: UUID, fact_id: UUID, statement: str, *, reason: str | None = None, via: Via = DIRECT
) -> Applied:
    """Replace a fact with the candidate's own words. The new fact is confirmed and carries no attributes (the old
    ones described the old words); the old row stays, superseded, and the change log keeps its quote beside the new."""
    statement = clean_text(statement, "statement")
    reason = optional_reason(reason)
    lock_record(conn, user_id)
    old = fact_row(conn, user_id, fact_id)
    if old.status not in ("unconfirmed", "confirmed"):
        raise StateConflict(old.status)
    new = _written_fact(uuid4(), old.kind, statement, _text_id(old.parent_fact_id), via)
    new_id = UUID(new["id"])
    before = fact_snapshot(old)
    event_id, version = _apply(
        conn, user_id, via, "fact", "update", new_id, before, new, reason=reason, confirms_fact=True
    )
    created_at = _insert_fact(conn, user_id, new, event_id)
    facts = models.profile_facts
    conn.execute(update(facts).where(facts.c.id == fact_id).values(superseded_at=func.now(), superseded_by=new_id))
    return Applied("fact", view(new, created_at), event_id, version)


@atomic
def reject_fact(
    conn: Connection, user_id: UUID, fact_id: UUID, *, reason: str | None = None, via: Via = DIRECT
) -> Applied:
    """Reject an unconfirmed fact, or retire a confirmed one (a fact cannot be both confirmed and rejected, so a
    confirmed one is superseded with no replacement and the log calls it a removal)."""
    reason = optional_reason(reason)
    lock_record(conn, user_id)
    fact = fact_row(conn, user_id, fact_id)
    before = fact_snapshot(fact)
    after: dict[str, Any] | None
    if fact.status == "unconfirmed":
        operation, after, column = "reject", {**before, "status": "rejected"}, "rejected_at"
    elif fact.status == "confirmed":
        operation, after, column = "remove", None, "superseded_at"
    else:
        raise StateConflict(fact.status)
    event_id, version = _apply(conn, user_id, via, "fact", operation, fact_id, before, after, reason=reason)
    conn.execute(update(models.profile_facts).where(models.profile_facts.c.id == fact_id).values({column: func.now()}))
    return Applied("fact", view(after or {**before, "status": "superseded"}, fact.created_at), event_id, version)


# ---- hard constraints ----------------------------------------------------------------------------


def _write_constraint(
    conn: Connection, user_id: UUID, clean: Mapping[str, Any], old: Row[Any] | None, via: Via
) -> Applied:
    """Create a constraint, or replace the active one of its kind. The old row is retired before the new one exists
    (one active row per kind), so its pointer to the replacement is filled in last; both columns are write-once."""
    new_id = uuid4()
    after = {"id": str(new_id), "source": via.source, **clean}
    before = None if old is None else constraint_snapshot(old)
    operation = "create" if old is None else "update"
    event_id, version = _apply(conn, user_id, via, "constraint", operation, new_id, before, after)
    table = models.constraints
    if old is not None:
        conn.execute(update(table).where(table.c.id == old.id).values(superseded_at=func.now()))
    floor = clean["salary_floor"]
    values = {name: clean[name] for name in ("kind", "statement", *TYPED_FIELDS) if name != "salary_floor"}
    values["salary_floor"] = None if floor is None else floor["amount"]
    values["salary_currency"] = None if floor is None else floor["currency"]
    created_at: datetime = conn.execute(
        insert(table)
        .values(
            id=new_id,
            user_id=user_id,
            source=via.source,
            profile_event_id=event_id,
            created_at=func.clock_timestamp(),
            **values,
        )
        .returning(table.c.created_at)
    ).scalar_one()
    if old is not None:
        conn.execute(update(table).where(table.c.id == old.id).values(superseded_by=new_id))
    return Applied("constraint", view(after, created_at), event_id, version)


@atomic
def set_constraint(
    conn: Connection, user_id: UUID, kind: str, fields: Mapping[str, Any], statement: str, *, via: Via = DIRECT
) -> Applied:
    """Make this the person's hard constraint of its kind: a new one, or the replacement of the active one."""
    clean = clean_constraint(kind, fields, statement)
    lock_record(conn, user_id)
    sql = f"""SELECT {CONSTRAINT_COLUMNS} FROM constraints c
              WHERE c.user_id = :u AND c.kind = :kind AND c.superseded_at IS NULL"""
    active = conn.execute(text(sql), {"u": user_id, "kind": clean["kind"]}).first()
    return _write_constraint(conn, user_id, clean, active, via)


def replace_constraint(
    conn: Connection, user_id: UUID, constraint_id: UUID, clean: Mapping[str, Any], via: Via
) -> Applied:
    """Replace one active constraint, named by id, with a whole new value (a proposal was checked to keep its kind)."""
    return _write_constraint(conn, user_id, clean, active_constraint(conn, user_id, constraint_id), via)


@atomic
def change_constraint(
    conn: Connection, user_id: UUID, constraint_id: UUID, changes: Mapping[str, Any], *, via: Via = DIRECT
) -> Applied:
    """Change fields of one active constraint (PATCH): what is not named stays. A constraint's kind never changes."""
    lock_record(conn, user_id)
    old = active_constraint(conn, user_id, constraint_id)
    if changes.get("kind", old.kind) != old.kind:
        raise InvalidField("kind", "cannot_change")
    now = constraint_snapshot(old)
    fields = {name: now[name] for name in TYPED_FIELDS}
    fields.update({name: value for name, value in changes.items() if name in TYPED_FIELDS and value is not None})
    clean = clean_constraint(old.kind, fields, changes.get("statement", old.statement))
    return _write_constraint(conn, user_id, clean, old, via)


@atomic
def remove_constraint(conn: Connection, user_id: UUID, constraint_id: UUID, *, via: Via = DIRECT) -> Applied:
    lock_record(conn, user_id)
    old = active_constraint(conn, user_id, constraint_id)
    before = constraint_snapshot(old)
    event_id, version = _apply(conn, user_id, via, "constraint", "remove", constraint_id, before, None)
    table = models.constraints
    conn.execute(update(table).where(table.c.id == constraint_id).values(superseded_at=func.now()))
    return Applied("constraint", view(before, old.created_at), event_id, version)


# ---- soft preferences ----------------------------------------------------------------------------


def _write_preference(
    conn: Connection, user_id: UUID, clean: Mapping[str, Any], old: Row[Any] | None, via: Via
) -> Applied:
    new_id = uuid4()
    after = {"id": str(new_id), **clean, "source": via.source}
    before = None if old is None else preference_snapshot(old)
    operation = "create" if old is None else "update"
    event_id, version = _apply(conn, user_id, via, "preference", operation, new_id, before, after)
    table = models.preferences
    created_at: datetime = conn.execute(
        insert(table)
        .values(
            id=new_id,
            user_id=user_id,
            source=via.source,
            profile_event_id=event_id,
            created_at=func.clock_timestamp(),
            **clean,
        )
        .returning(table.c.created_at)
    ).scalar_one()
    if old is not None:
        conn.execute(update(table).where(table.c.id == old.id).values(superseded_at=func.now(), superseded_by=new_id))
    shown = preference_view(after, created_at)
    if clean["kind"] == "untyped_dealbreaker":
        shown["suggested_constraint"] = suggested_constraint(clean["statement"])  # offered, never applied
    return Applied("preference", shown, event_id, version)


@atomic
def add_preference(
    conn: Connection,
    user_id: UUID,
    kind: str,
    statement: str,
    *,
    value: str | None = None,
    weight: int | None = None,
    via: Via = DIRECT,
) -> Applied:
    """A soft preference. A dealbreaker is stored as untyped_dealbreaker at weight -3 (REC-04); when it fits a hard
    constraint kind the result carries suggested_constraint, and nothing is applied until the candidate confirms it."""
    clean = clean_preference(kind, statement, value, weight)
    lock_record(conn, user_id)
    return _write_preference(conn, user_id, clean, None, via)


def replace_preference(
    conn: Connection, user_id: UUID, preference_id: UUID, clean: Mapping[str, Any], via: Via
) -> Applied:
    """Replace one active preference, named by id, with a whole new value (a proposal was checked to keep its kind)."""
    return _write_preference(conn, user_id, clean, active_preference(conn, user_id, preference_id), via)


@atomic
def change_preference(
    conn: Connection, user_id: UUID, preference_id: UUID, changes: Mapping[str, Any], *, via: Via = DIRECT
) -> Applied:
    """Change a preference (PATCH): what is not named stays. A dealbreaker's value follows its new words unless the
    value is given."""
    lock_record(conn, user_id)
    old = active_preference(conn, user_id, preference_id)
    if changes.get("kind", old.kind) != old.kind:
        raise InvalidField("kind", "cannot_change")
    follows_words = old.kind == "untyped_dealbreaker" and "statement" in changes
    value = changes.get("value", None if follows_words else old.value)
    statement, weight = changes.get("statement", old.statement), changes.get("weight", old.weight)
    return _write_preference(conn, user_id, clean_preference(old.kind, statement, value, weight), old, via)


@atomic
def remove_preference(conn: Connection, user_id: UUID, preference_id: UUID, *, via: Via = DIRECT) -> Applied:
    lock_record(conn, user_id)
    old = active_preference(conn, user_id, preference_id)
    before = preference_snapshot(old)
    event_id, version = _apply(conn, user_id, via, "preference", "remove", preference_id, before, None)
    table = models.preferences
    conn.execute(update(table).where(table.c.id == preference_id).values(superseded_at=func.now()))
    return Applied("preference", preference_view(before, old.created_at), event_id, version)


# ---- reads for the candidate's own pages ---------------------------------------------------------


def encode_cursor(*parts: Any) -> str:
    return base64.urlsafe_b64encode(json.dumps(parts).encode()).decode().rstrip("=")


def decode_cursor(cursor: str, *parsers: Callable[[Any], Any]) -> list[Any]:
    """The values a cursor holds, parsed; a cursor this API did not issue is a 400."""
    try:
        parts = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(parts, list) or len(parts) != len(parsers):
            raise ValueError
        return [parse(part) for parse, part in zip(parsers, parts, strict=True)]
    except (ValueError, TypeError, binascii.Error) as error:
        raise BadCursor() from error


def page(
    rows: Sequence[Row[Any]], limit: int, key: Callable[[Row[Any]], Sequence[Any]]
) -> tuple[list[Row[Any]], str | None]:
    """One more row than `limit` was asked for: the first `limit` are the page, and the extra one means a next page."""
    kept = list(rows[:limit])
    return kept, (encode_cursor(*key(kept[-1])) if len(rows) > limit else None)


def list_facts(
    conn: Connection, user_id: UUID, *, status: str | None = None, limit: int = 20, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """The person's facts of every status, or of one, in the order they were made (a parse's in document order)."""
    where: list[str] = ["f.user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    if status is not None:
        where.append(_FACT_STATUS_WHERE[one_of(status, tuple(_FACT_STATUS_WHERE), "status")])
    if cursor is not None:
        params["c_at"], params["c_span"], params["c_id"] = decode_cursor(cursor, datetime.fromisoformat, int, UUID)
        where.append(f"({FACT_ORDER}) > (:c_at, :c_span, :c_id)")
    sql = f"SELECT {FACT_COLUMNS} FROM profile_facts f WHERE {' AND '.join(where)} ORDER BY {FACT_ORDER} LIMIT :n"
    rows = conn.execute(text(sql), params).all()
    kept, next_cursor = page(
        rows, limit, lambda r: (r.created_at.isoformat(), MAX_INT4 if r.span_start is None else r.span_start, str(r.id))
    )
    return [view(fact_snapshot(row), row.created_at) for row in kept], next_cursor


def facts_by_ids(conn: Connection, user_id: UUID, ids: Sequence[UUID]) -> list[dict[str, Any]]:
    """The person's facts with these ids that were ever confirmed, whatever their state now. A verdict cites a fact
    while it is confirmed, and must still show it after the person corrected or retired it. A fact waiting for review,
    or rejected before it was confirmed, is never returned (REC-02)."""
    sql = f"""SELECT {FACT_COLUMNS} FROM profile_facts f
              WHERE f.user_id = :u AND f.id = ANY(:ids) AND f.confirmed_at IS NOT NULL ORDER BY {FACT_ORDER}"""
    return [
        view(fact_snapshot(row), row.created_at) for row in conn.execute(text(sql), {"u": user_id, "ids": list(ids)})
    ]


def list_constraints(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """The active hard constraints: at most one per kind, so there is no paging."""
    sql = f"""SELECT {CONSTRAINT_COLUMNS} FROM constraints c
              WHERE c.user_id = :u AND c.superseded_at IS NULL ORDER BY c.created_at, c.id"""
    return [view(constraint_snapshot(row), row.created_at) for row in conn.execute(text(sql), {"u": user_id})]


def list_preferences(
    conn: Connection, user_id: UUID, *, limit: int = 20, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    where: list[str] = ["p.user_id = :u", "p.superseded_at IS NULL"]
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    if cursor is not None:
        params["c_at"], params["c_id"] = decode_cursor(cursor, datetime.fromisoformat, UUID)
        where.append("(p.created_at, p.id) > (:c_at, :c_id)")
    sql = f"""SELECT {PREFERENCE_COLUMNS} FROM preferences p WHERE {" AND ".join(where)}
              ORDER BY p.created_at, p.id LIMIT :n"""
    kept, next_cursor = page(
        conn.execute(text(sql), params).all(), limit, lambda r: (r.created_at.isoformat(), str(r.id))
    )
    return [preference_view(preference_snapshot(row), row.created_at) for row in kept], next_cursor


def record_header(conn: Connection, user_id: UUID) -> Row[Any]:
    """The version of the record and the two counts the candidate is told about: facts waiting for review and
    proposals waiting for an answer."""
    row = conn.execute(
        text(
            """SELECT u.record_version,
                      (SELECT count(*) FROM profile_facts f WHERE f.user_id = u.id
                          AND f.confirmed_at IS NULL AND f.rejected_at IS NULL AND f.superseded_at IS NULL)
                        AS unconfirmed_facts,
                      (SELECT count(*) FROM profile_events e WHERE e.user_id = u.id AND e.event_type = 'proposed'
                          AND NOT EXISTS (SELECT 1 FROM profile_events r WHERE r.proposal_event_id = e.id))
                        AS open_proposals
               FROM users u WHERE u.id = :u"""
        ),
        {"u": user_id},
    ).first()
    if row is None:
        raise RecordNotFound()
    return row


def record_documents(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """The person's latest uploads, without their content: name, type, size and how far parsing got."""
    rows = conn.execute(
        text(
            """SELECT id, original_filename, mime_type, byte_size, status, parse_error_code, hidden_text_chars,
                      created_at, parsed_at
               FROM documents WHERE user_id = :u ORDER BY created_at DESC, id DESC LIMIT :n"""
        ),
        {"u": user_id, "n": RECENT_DOCUMENTS},
    ).all()
    return [{**row._mapping, "status": models.DOCUMENT_API_STATUS[row.status]} for row in rows]
