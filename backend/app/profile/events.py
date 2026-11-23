"""Proposals and the change log (REC-05, CHT-03; DATA_MODEL 3.4 profile_events).

A change that someone other than the candidate suggests waits as a `proposed` row until the candidate confirms it, which
applies it, or declines it. Nothing else changes the record. The log lists what happened: applied changes with their
before and after, declined and expired proposals.

This module writes and reads profile_events. It publishes nothing: Phase 1 has no event bus and nothing consumes record
changes before Phase 4, so the row and the version on users are the signal (see app.profile.service).
"""

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Row, func, insert, text

from . import models, service
from .errors import AlreadyResolved, InvalidField, RecordNotFound, StateConflict
from .service import Applied, Via

OPERATIONS = ("create", "update", "remove")  # a proposal never confirms or rejects: those are the candidate's own


def _target_snapshot(conn: Connection, user_id: UUID, entity: str, entity_id: UUID) -> dict[str, Any]:
    """The row an update or a removal would change, as it is now. Only a live row can be changed."""
    if entity == "fact":
        fact = service.fact_row(conn, user_id, entity_id)
        if fact.status not in ("unconfirmed", "confirmed"):
            raise StateConflict(fact.status)
        return service.fact_snapshot(fact)
    if entity == "constraint":
        return service.constraint_snapshot(service.active_constraint(conn, user_id, entity_id))
    return service.preference_snapshot(service.active_preference(conn, user_id, entity_id))


def _clean_after(entity: str, operation: str, after: object, target: dict[str, Any] | None) -> dict[str, Any]:
    """What a proposal would write, checked by the same rules as a direct change and with nothing extra allowed."""
    if not isinstance(after, Mapping):
        raise InvalidField("after", "required")
    if entity == "fact":
        service.only_keys(after, ("kind", "statement") if operation == "create" else ("statement",))
        clean = {"statement": service.clean_text(after.get("statement"), "statement")}
        if operation == "create":
            clean["kind"] = service.one_of(after.get("kind"), models.FACT_KINDS, "kind")
        return clean
    if entity == "constraint":
        service.only_keys(after, ("kind", "statement", *service.TYPED_FIELDS))
        fields = {name: value for name, value in after.items() if name not in ("kind", "statement")}
        clean = service.clean_constraint(after.get("kind"), fields, after.get("statement"))
    else:
        service.only_keys(after, ("kind", "value", "weight", "statement"))
        clean = service.clean_preference(
            after.get("kind"), after.get("statement"), after.get("value"), after.get("weight")
        )
    if target is not None and clean["kind"] != target["kind"]:
        raise InvalidField("kind", "cannot_change")
    return clean


@service.atomic
def propose_change(
    conn: Connection,
    user_id: UUID,
    *,
    origin: str,
    entity: str,
    operation: str,
    after: Mapping[str, Any] | None = None,
    entity_id: UUID | None = None,
    origin_ref: str | None = None,
    reason: str | None = None,
) -> UUID:
    """Record a change that someone other than the candidate suggests. It changes nothing else: the record is as it was
    until the candidate confirms. What it would write is checked now, so a protected characteristic or a malformed
    value is refused here and nothing is stored. A create names no row; an update or a removal names the row it changes.
    Returns the proposal's event id."""
    origin = service.one_of(origin, models.PROPOSAL_ORIGINS, "origin")
    entity = service.one_of(entity, models.PROFILE_ENTITIES, "entity")
    operation = service.one_of(operation, OPERATIONS, "operation")
    reason = service.optional_reason(reason)
    if origin_ref is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", origin_ref):
        raise InvalidField("origin_ref", "invalid_value")  # an id, never text
    if (operation == "create") != (entity_id is None):
        raise InvalidField("entity_id", "required" if entity_id is None else "invalid_value")
    if operation == "remove" and after is not None:
        raise InvalidField("after", "invalid_value")
    service.lock_record(conn, user_id)
    before = None if entity_id is None else _target_snapshot(conn, user_id, entity, entity_id)
    clean = None if operation == "remove" else _clean_after(entity, operation, after, before)
    proposal_id: UUID = conn.execute(
        insert(models.profile_events)
        .values(
            user_id=user_id,
            event_type="proposed",
            entity=entity,
            operation=operation,
            entity_id=entity_id,
            before=before,
            after=clean,
            origin=origin,
            origin_ref=origin_ref,
            reason=reason,
            created_at=func.clock_timestamp(),
        )
        .returning(models.profile_events.c.id)
    ).scalar_one()
    return proposal_id


def _open_proposal(conn: Connection, user_id: UUID, event_id: UUID) -> Row[Any]:
    """A proposal of this person that has no answer yet: 404 for any other id, 409 when it has been answered."""
    proposal = conn.execute(
        text(
            """SELECT id, entity, operation, entity_id, before, after, reason FROM profile_events
               WHERE id = :id AND user_id = :u AND event_type = 'proposed'"""
        ),
        {"id": event_id, "u": user_id},
    ).first()
    if proposal is None:
        raise RecordNotFound()
    answer = conn.execute(
        text("SELECT event_type FROM profile_events WHERE proposal_event_id = :id"), {"id": event_id}
    ).scalar()
    if answer is not None:
        raise AlreadyResolved(answer)
    return proposal


@service.atomic
def confirm(conn: Connection, user_id: UUID, event_id: UUID) -> Applied:
    """Apply a proposal. The candidate's confirmation is the only thing that changes the record: the change is written
    as a confirmed_proposal that points at its proposal, which is thereby answered (once; the table's unique index
    refuses a second answer). If the row it targets has since changed, it can no longer be applied: decline it."""
    service.lock_record(conn, user_id)
    proposal = _open_proposal(conn, user_id, event_id)
    via = Via("confirmed_proposal", proposal.id, proposal.reason)
    after, target, operation = proposal.after, proposal.entity_id, proposal.operation
    if proposal.entity == "fact":
        if operation == "create":
            return service.add_fact(conn, user_id, after["kind"], after["statement"], via=via)
        if operation == "update":
            return service.correct_fact(conn, user_id, target, after["statement"], via=via)
        return service.reject_fact(conn, user_id, target, via=via)
    if proposal.entity == "constraint":
        if operation == "remove":
            return service.remove_constraint(conn, user_id, target, via=via)
        fields = {name: value for name, value in after.items() if name not in ("kind", "statement")}
        if operation == "create":
            return service.set_constraint(conn, user_id, after["kind"], fields, after["statement"], via=via)
        clean = service.clean_constraint(after["kind"], fields, after["statement"])
        return service.replace_constraint(conn, user_id, target, clean, via)
    if operation == "remove":
        return service.remove_preference(conn, user_id, target, via=via)
    value, weight = after["value"], after["weight"]
    if operation == "create":
        return service.add_preference(
            conn, user_id, after["kind"], after["statement"], value=value, weight=weight, via=via
        )
    clean = service.clean_preference(after["kind"], after["statement"], value, weight)
    return service.replace_preference(conn, user_id, target, clean, via)


@service.atomic
def decline(conn: Connection, user_id: UUID, event_id: UUID) -> UUID:
    """Answer a proposal with no. The record does not change, so neither does its version. Returns the new row's id."""
    service.lock_record(conn, user_id)
    proposal = _open_proposal(conn, user_id, event_id)
    declined_id: UUID = conn.execute(
        insert(models.profile_events)
        .values(
            user_id=user_id,
            event_type="declined",
            entity=proposal.entity,
            operation=proposal.operation,
            entity_id=proposal.entity_id,
            before=proposal.before,
            after=proposal.after,
            proposal_event_id=proposal.id,
            reason=proposal.reason,
            created_at=func.clock_timestamp(),
        )
        .returning(models.profile_events.c.id)
    ).scalar_one()
    return declined_id


def list_events(
    conn: Connection, user_id: UUID, *, status: str | None = None, limit: int = 20, cursor: str | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """The change log, newest first: what was applied, declined or expired. status=proposed lists the proposals still
    waiting for an answer instead; an answered proposal appears as its answer, which names where it came from."""
    where: list[str] = ["e.user_id = :u"]
    params: dict[str, Any] = {"u": user_id, "n": limit + 1}
    if status is None:
        where.append("e.event_type <> 'proposed'")
    elif status == "proposed":
        where.append(
            "e.event_type = 'proposed' AND NOT EXISTS (SELECT 1 FROM profile_events r WHERE r.proposal_event_id = e.id)"
        )
    else:
        where.append("e.event_type = :status")
        params["status"] = service.one_of(status, models.PROFILE_EVENT_TYPES, "status")
    if cursor is not None:
        params["c_at"], params["c_id"] = service.decode_cursor(cursor, datetime.fromisoformat, UUID)
        where.append("(e.created_at, e.id) < (:c_at, :c_id)")
    sql = f"""SELECT e.id, e.event_type AS status, e.entity, e.operation, e.entity_id, e.before, e.after, e.source,
                     COALESCE(e.origin, p.origin) AS origin, e.proposal_event_id, e.reason, e.record_version_after,
                     e.created_at
              FROM profile_events e LEFT JOIN profile_events p ON p.id = e.proposal_event_id
              WHERE {" AND ".join(where)} ORDER BY e.created_at DESC, e.id DESC LIMIT :n"""
    rows = conn.execute(text(sql), params).all()
    kept, next_cursor = service.page(rows, limit, lambda r: (r.created_at.isoformat(), str(r.id)))
    return [dict(row._mapping) for row in kept], next_cursor
