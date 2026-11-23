"""The registry ownership test (TRU-02; ARCHITECTURE 8.4, test 2; DATA_MODEL 7.2).

Every table in the database is owned by exactly one module, which registered it with trust.privacy.register and said
what erasure does to it, or it is on an explicit list of tables that hold nothing about a person. So a table added
without a deleter fails here, naming it. The registrations are made when the two composition roots are imported, so
both are imported first: that is the registry the running system has.

The checker is a plain function of three things: the schema, the registry and the allow-list. The real test gives it
the live ones; the self-tests give it a schema with a mistake built into it (inside the test's transaction, rolled
back) or a registry with one, and show it reports them.
"""

import importlib
from typing import NamedTuple

import pytest
from sqlalchemy import text

from app.trust import privacy

importlib.import_module("app.main")  # the API's registrations ...
importlib.import_module("app.worker")  # ... and the worker's, which is where the export job runs

# Tables that hold nothing about a person and so need no deleter. The five ingest tables are registered by
# app.ingest.privacy, and model_registry and eval_runs by the platform; all are listed again here so a deleted
# registration cannot hide them. alembic_version is Alembic's own bookkeeping.
NO_PERSONAL_DATA = {
    "alembic_version",
    "job_sources",
    "jobs",
    "job_requirements",
    "fetch_runs",
    "skills",
    "model_registry",
    "eval_runs",
}
# A table with a foreign key to users that erasure keeps on purpose: the bare id, so a restore can be erased again.
KEPT_WITH_USER_KEY = {"data_requests"}
HOW_TO_FIX = (
    "Register it with trust.privacy.register in the module that owns it (tables, with what erasure does to each, and "
    "an exporter). If it holds nothing about a person, list it in NO_PERSONAL_DATA in this test with the reason."
)

SCHEMA_TABLES = "SELECT schemaname, tablename FROM pg_tables WHERE schemaname IN ('public', 'langgraph')"
FOREIGN_KEYS = """
    SELECT child_ns.nspname AS child_schema, child.relname AS child, parent_ns.nspname AS parent_schema,
           parent.relname AS parent, c.confdeltype AS action
    FROM pg_constraint c
    JOIN pg_class child ON child.oid = c.conrelid
    JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
    JOIN pg_class parent ON parent.oid = c.confrelid
    JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
    WHERE c.contype = 'f' AND child_ns.nspname IN ('public', 'langgraph')
"""


def named(schema: str, table: str) -> str:
    return table if schema == "public" else f"{schema}.{table}"


class Schema(NamedTuple):
    tables: set[str]
    keys: list[tuple[str, str, str]]  # (child, parent, action) for every foreign key; action as in pg_constraint


def read_schema(conn) -> Schema:
    tables = {named(row.schemaname, row.tablename) for row in conn.execute(text(SCHEMA_TABLES))}
    keys = [
        (named(k.child_schema, k.child), named(k.parent_schema, k.parent), k.action)
        for k in conn.execute(text(FOREIGN_KEYS))
    ]
    return Schema(tables, keys)


def cascading_tables(schema: Schema) -> set[str]:
    """The tables whose rows go when a user goes: users itself, and any table with a CASCADE key to one of those."""
    reached = {"users"}
    while True:
        more = {child for child, parent, action in schema.keys if action == "c" and parent in reached} - reached
        if not more:
            return reached
        reached |= more


def ownership_problems(
    schema: Schema, registry: dict[str, dict[str, str]], exporters: set[str], no_personal_data: set[str]
) -> list[str]:
    """Every way the registry and the schema disagree, each as a sentence naming the table (or the module)."""
    problems = []
    owners: dict[str, list[str]] = {}
    for module, tables in registry.items():
        for table in tables:
            owners.setdefault(table, []).append(module)
    for table in sorted(schema.tables - set(owners) - no_personal_data):
        problems.append(f"{table}: no module registered it. {HOW_TO_FIX}")
    for table, modules in sorted(owners.items()):
        if len(modules) > 1:
            problems.append(
                f"{table}: registered by {len(modules)} modules ({', '.join(sorted(modules))}); one may own it"
            )
        if table not in schema.tables:
            problems.append(f"{table}: registered by {modules[0]} but there is no such table in the database")

    cascading = cascading_tables(schema)
    to_users = {}
    for child, parent, action in schema.keys:
        if parent == "users":
            to_users.setdefault(child, set()).add(action)
    for table, modules in sorted(owners.items()):
        effect = registry[modules[0]][table]
        if effect == "cascade" and table not in cascading:
            problems.append(f"{table}: registered cascade, but no chain of CASCADE foreign keys leads from it to users")
        if effect == "set_null" and "n" not in to_users.get(table, set()):
            problems.append(f"{table}: registered set_null, but it has no SET NULL foreign key to users")
        if effect == "set_null" and "c" in to_users.get(table, set()):
            problems.append(f"{table}: registered set_null, but a CASCADE foreign key to users deletes its rows")
    for table in sorted(to_users):
        effect = registry[owners[table][0]][table] if table in owners else None
        if table in no_personal_data or effect == "no_personal_data":
            problems.append(f"{table}: has a foreign key to users but is declared to hold no personal data")
        elif effect not in (None, "cascade", "set_null") and table not in KEPT_WITH_USER_KEY:
            problems.append(
                f"{table}: has a foreign key to users, so erasure must be cascade or set_null, not {effect}"
            )

    # ARCHITECTURE 8.3 and 8.4: what is erased can be exported, so a module with a person's data has an exporter.
    for module, tables in sorted(registry.items()):
        holds_people = any(effect != "no_personal_data" for effect in tables.values())
        if holds_people and module not in exporters:
            problems.append(f"{module}: owns tables with personal data but registered no exporter (TRU-01)")
    return problems


def live_problems(conn) -> list[str]:
    return ownership_problems(read_schema(conn), privacy.registry(), set(privacy.exporters()), NO_PERSONAL_DATA)


def make_probe(conn, name: str = "orphan_probe", references: str = "users(id)", action: str = "") -> None:
    conn.execute(text(f"CREATE TABLE {name} (id uuid PRIMARY KEY, user_id uuid REFERENCES {references} {action})"))


# ---- the real registry --------------------------------------------------------------------------------


def test_every_table_is_owned_by_exactly_one_module_and_the_registry_agrees_with_the_schema(conn):
    problems = live_problems(conn)
    assert not problems, "the deletion registry and the schema disagree:\n  " + "\n  ".join(problems)


def test_the_registry_the_roots_build_holds_the_tables_every_module_owns(conn):
    owned = {table for tables in privacy.registry().values() for table in tables}
    assert {"users", "consents", "audit_log", "data_requests", "documents", "profile_facts", "eval_items"} <= owned
    assert {"job_sources", "jobs", "fetch_runs", "skills", "job_requirements"} <= owned  # registered, not only listed
    assert set(privacy.registry()) >= {"platform", "evals", "trust", "profile", "ingest"}
    assert {"platform", "evals", "trust", "profile"} <= set(privacy.exporters())


def test_the_allow_list_names_no_table_that_holds_a_person(conn):
    schema = read_schema(conn)
    referencing = {child for child, parent, _ in schema.keys if parent == "users"}
    assert referencing.isdisjoint(NO_PERSONAL_DATA)


# ---- the checker can fail -----------------------------------------------------------------------------


def test_a_table_nobody_registered_is_reported_by_name(conn):
    make_probe(conn)  # orphan_probe(user_id uuid REFERENCES users(id))
    problems = live_problems(conn)
    assert [p for p in problems if p.startswith("orphan_probe: no module registered it")]
    assert "NO_PERSONAL_DATA" in problems[0]  # the message says what to do about it


def test_a_table_with_no_user_column_at_all_is_reported_too(conn):
    conn.execute(text("CREATE TABLE plain_probe (id uuid PRIMARY KEY, note text)"))
    assert [p for p in live_problems(conn) if p.startswith("plain_probe: no module registered it")]


def registry_with(extra: dict[str, str], module: str = "probe") -> dict[str, dict[str, str]]:
    return {**privacy.registry(), module: extra}


def problems_for(conn, registry, exporters=None) -> list[str]:
    return ownership_problems(
        read_schema(conn),
        registry,
        set(privacy.exporters()) | {"probe"} if exporters is None else exporters,
        NO_PERSONAL_DATA,
    )


def test_a_table_registered_by_two_modules_is_reported_naming_both(conn):
    make_probe(conn)
    registry = {**registry_with({"orphan_probe": "cascade"}), "second": {"orphan_probe": "cascade"}}
    assert [
        p
        for p in problems_for(conn, registry)
        if "orphan_probe" in p and "probe, second" in p and "one may own it" in p
    ]


def test_a_registration_for_a_table_that_does_not_exist_is_reported(conn):
    assert [
        p for p in problems_for(conn, registry_with({"ghost": "cascade"})) if p.startswith("ghost: registered by probe")
    ]


@pytest.mark.parametrize("effect", ["kept", "by_email", "trigger", "no_personal_data"])
def test_a_table_with_a_user_key_must_be_cascade_or_set_null(conn, effect):
    make_probe(conn, action="ON DELETE CASCADE")
    problems = [p for p in problems_for(conn, registry_with({"orphan_probe": effect})) if p.startswith("orphan_probe")]
    assert problems, effect  # the one table kept with a user key, data_requests, is the only exception (below)


def test_the_one_exception_is_data_requests_kept_with_the_bare_id(conn):
    assert live_problems(conn) == []
    registry = {**privacy.registry()}
    registry["trust"] = {**registry["trust"], "data_requests": "cascade"}  # a mistake there is reported, not the effect
    assert [p for p in problems_for(conn, registry) if p.startswith("data_requests: registered cascade")]


def test_cascade_must_be_true_of_the_foreign_key(conn):
    make_probe(conn, action="ON DELETE SET NULL")
    problems = problems_for(conn, registry_with({"orphan_probe": "cascade"}))
    assert [p for p in problems if p.startswith("orphan_probe: registered cascade, but no chain")]


def test_set_null_must_be_true_of_the_foreign_key(conn):
    make_probe(conn, action="ON DELETE CASCADE")
    problems = problems_for(conn, registry_with({"orphan_probe": "set_null"}))
    assert [p for p in problems if p.startswith("orphan_probe: registered set_null, but")]
    make_probe(conn, "other_probe", action="")  # NO ACTION: no SET NULL either
    assert [p for p in problems_for(conn, registry_with({"other_probe": "set_null"})) if p.startswith("other_probe")]


def test_a_table_reached_through_a_parent_is_cascade_only_if_every_link_cascades(conn):
    make_probe(conn, "parent_probe", action="ON DELETE CASCADE")
    make_probe(conn, "child_probe", "parent_probe(id)", "ON DELETE CASCADE")
    both = registry_with({"parent_probe": "cascade", "child_probe": "cascade"})
    assert not [p for p in problems_for(conn, both) if "probe" in p.split(":")[0] and "registered cascade" in p]
    conn.execute(
        text("ALTER TABLE child_probe DROP CONSTRAINT child_probe_user_id_fkey")
    )  # child links to the parent only
    conn.execute(text("ALTER TABLE child_probe ADD COLUMN parent_id uuid REFERENCES parent_probe(id)"))  # NO ACTION
    assert [p for p in problems_for(conn, both) if p.startswith("child_probe: registered cascade")]


def test_a_module_with_personal_data_and_no_exporter_is_reported(conn):
    make_probe(conn, action="ON DELETE CASCADE")
    registry = registry_with({"orphan_probe": "cascade"})
    assert not [p for p in problems_for(conn, registry) if p.startswith("probe:")]
    assert [p for p in problems_for(conn, registry, exporters={"platform"}) if p.startswith("probe: owns tables")]
    # a module whose tables hold nothing needs none
    quiet = registry_with({"orphan_probe": "no_personal_data"})
    assert not [p for p in problems_for(conn, quiet, exporters=set(privacy.exporters())) if p.startswith("probe: owns")]


def test_the_cascade_reach_follows_foreign_keys_through_parents():
    schema = Schema({"users", "a", "b", "c"}, [("a", "users", "c"), ("b", "a", "c"), ("c", "b", "n")])
    assert cascading_tables(schema) == {"users", "a", "b"}  # c is only SET NULL from b: its rows stay
