"""What other modules may call from app.profile (the shared contracts in the Phase 3 index). Nothing else in this
package is a public interface, and a test fails if another module imports anything but this file (ARCHITECTURE
3.3, rule 1).

Every read here returns the usable record only: facts that are confirmed, not rejected and not superseded (REC-02), and
the active constraints and preferences. Facts waiting for review are counted, never returned, so no module can use one
by accident. The predicate is the one of the partial index profile_facts_usable_idx (service.USABLE_FACT).

  get_record(conn, user_id)           the record as one object, the consumer section of GET /v1/me/record
  usable_facts(conn, user_id)         the confirmed facts, with the source span and quote of a resume fact
  compiled_constraints(conn, user_id) the active hard constraints as plain typed values, by kind
  facts_by_ids(conn, user_id, ids)    facts a verdict cited, by id, once confirmed and since corrected or retired too
  propose_change(conn, user_id, ...)  suggest a record change; it waits for the candidate (CHT-03)
  confirm(conn, user_id, event_id)    apply a proposal the candidate confirmed
  declined_since(conn, user_id, since) the proposals the candidate declined since a time, so they are not repeated
  screen(text)                        the protected-characteristic screen (REC-07): a category name, or None

Each but screen takes an open connection, as the caller owns the transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from . import events, service
from .events import propose_change
from .protected import screen

__all__ = [
    "compiled_constraints",
    "confirm",
    "declined_since",
    "facts_by_ids",
    "get_record",
    "propose_change",
    "screen",
    "usable_facts",
]

PAGE = 100


@dataclass(frozen=True)
class Confirmed:
    """What applying a proposal changed, by id: the kind of row (fact, constraint or preference), its id and the version
    of the record after it. No row is returned, so what a confirmation retired never leaves this module."""

    entity: str
    entity_id: UUID
    record_version: int


def confirm(conn: Connection, user_id: UUID, event_id: UUID) -> Confirmed:
    """Apply a proposal the candidate confirmed (CHT-03). Raises 404 when it is not theirs, 409 when it was answered."""
    applied = events.confirm(conn, user_id, event_id)
    return Confirmed(applied.entity, UUID(applied.row["id"]), applied.record_version)


@dataclass(frozen=True)
class DeclinedProposal:
    """A proposal the candidate answered with no: the kind of row (fact, constraint or preference), what the change was
    (create, update or remove), the row's `field` (its kind: a constraint or preference kind, or a fact kind), `value`
    (what it would have written, or for a removal what it would have removed, as the change log stores it) and when."""

    entity: str
    operation: str
    field: str
    value: dict[str, Any]
    declined_at: datetime


def declined_since(conn: Connection, user_id: UUID, since: datetime) -> list[DeclinedProposal]:
    """The person's declined proposals from `since` on, newest first. A proposal still waiting, or answered with yes,
    is not here. It lets a proposer see what the person already said no to and not offer it again."""
    rows = conn.execute(
        text(
            """SELECT entity::text AS entity, operation::text AS operation, before, after, created_at
                 FROM profile_events
                WHERE user_id = :u AND event_type = 'declined' AND created_at >= :since
                ORDER BY created_at DESC, id DESC"""
        ),
        {"u": user_id, "since": since},
    )
    declined = []
    for row in rows:
        # A creation or an update was declined with its `after`; a removal has only the row it would have removed.
        value = row.after if row.after is not None else row.before
        field = (value or {}).get("kind") or (row.before or {}).get("kind")  # a fact update's `after` has no kind
        if value is not None and field is not None:
            declined.append(DeclinedProposal(row.entity, row.operation, field, value, row.created_at))
    return declined


def usable_facts(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    """The person's confirmed facts, in the order they were made. Never an unconfirmed, rejected or superseded one."""
    sql = f"""SELECT {service.FACT_COLUMNS} FROM profile_facts f
              WHERE f.user_id = :u AND {service.USABLE_FACT} ORDER BY {service.FACT_ORDER}"""
    return [service.view(service.fact_snapshot(row), row.created_at) for row in conn.execute(text(sql), {"u": user_id})]


def facts_by_ids(conn: Connection, user_id: UUID, ids: Sequence[UUID]) -> list[dict[str, Any]]:
    """The person's facts with these ids, in the shape of usable_facts, if they were ever confirmed. Unlike every other
    read here it also returns a fact that is no longer usable (corrected or retired since): its `status` says so, and
    only `confirmed` is current. It exists so that a stored verdict can still show the fact it cited. A fact that was
    never confirmed, and any fact of another person, is not returned."""
    return service.facts_by_ids(conn, user_id, ids)


def compiled_constraints(conn: Connection, user_id: UUID) -> dict[str, dict[str, Any]]:
    """The active hard constraints by kind: the constraint's id and the typed values of its kind. A kind the person has
    not set is absent: nothing is defaulted, so an absent kind means no constraint of that kind."""
    return {
        constraint["kind"]: {
            "id": constraint["id"],
            **{name: constraint[name] for name in service.KIND_FIELDS[constraint["kind"]]},
        }
        for constraint in service.list_constraints(conn, user_id)
    }


def _active_preferences(conn: Connection, user_id: UUID) -> list[dict[str, Any]]:
    preferences: list[dict[str, Any]] = []
    cursor = None
    while True:
        batch, cursor = service.list_preferences(conn, user_id, limit=PAGE, cursor=cursor)
        preferences += batch
        if cursor is None:
            return preferences


def get_record(conn: Connection, user_id: UUID) -> dict[str, Any]:
    """The Career Record as one object: the record version, the confirmed facts with their sources, the active
    constraints and preferences, and how many facts and proposals are waiting for the candidate (counts only)."""
    header = service.record_header(conn, user_id)
    return {
        "record_version": int(header.record_version),
        "facts": usable_facts(conn, user_id),
        "constraints": service.list_constraints(conn, user_id),
        "preferences": _active_preferences(conn, user_id),
        "unconfirmed_facts": int(header.unconfirmed_facts),
        "open_proposals": int(header.open_proposals),
    }
