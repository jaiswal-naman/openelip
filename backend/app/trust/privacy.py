"""The deletion registry (ARCHITECTURE 8.4; DATA_MODEL 7.2): who owns each table and what erasure does to it.

A module calls register() once at import time with its tables and, when it holds a person's data, an exporter that the
export job calls (ARCHITECTURE 8.3). The registry tests read registry() and fail on a table nobody registered, so a new
table cannot reach production with no deleter. Registering a table twice is an error: exactly one module owns it.

A table with both a cascade and a SET NULL reference to users (audit_log) is listed under cascade, the effect on the
person's own rows; the catalogue test checks the column-level references.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import Connection

EFFECTS = (
    "cascade",  # the person's rows go with the user row
    "set_null",  # the row stays, its reference to the person is cleared (the cost ledger)
    "by_email",  # no foreign key; erase_user deletes by the person's address
    "trigger",  # no foreign key; a database trigger deletes it
    "kept",  # kept on purpose (data_requests holds the bare id so a restore can be erased again)
    "no_personal_data",  # holds nothing about a person
)


@dataclass(frozen=True)
class ModuleExport:
    """What one module adds to a person's export: JSON-ready rows by table name, and files by path in the archive."""

    data: dict[str, Any]
    files: dict[str, bytes] = field(default_factory=dict)


Exporter = Callable[[Connection, UUID], ModuleExport]

_tables: dict[str, dict[str, str]] = {}  # module -> table -> effect
_exporters: dict[str, Exporter] = {}  # module -> its export function


def register(module: str, tables: Mapping[str, str], exporter: Exporter | None = None) -> None:
    """Record that `module` owns `tables`, each with its effect. Nothing is recorded if any entry is refused."""
    unknown = sorted(table for table, effect in tables.items() if effect not in EFFECTS)
    if unknown:
        raise ValueError(f"{module}: not a deletion effect for {', '.join(unknown)}; use one of {', '.join(EFFECTS)}")
    owners = {table: owner for owner, owned in _tables.items() for table in owned}
    taken = sorted(table for table in tables if table in owners)
    if taken:
        raise ValueError("already registered: " + ", ".join(f"{table} by {owners[table]}" for table in taken))
    if exporter is not None and module in _exporters:
        raise ValueError(f"{module} already has an exporter")
    _tables.setdefault(module, {}).update(tables)
    if exporter is not None:
        _exporters[module] = exporter


def registry() -> dict[str, dict[str, str]]:
    """Every registered table by owning module, with what erasure does to it."""
    return {module: dict(tables) for module, tables in _tables.items()}


def exporters() -> dict[str, Exporter]:
    """The export function of each module that registered one, by module name."""
    return dict(_exporters)
