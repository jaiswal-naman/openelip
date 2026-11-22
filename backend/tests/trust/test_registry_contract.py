"""The deletion registry contract (TRU-02, ARCHITECTURE 8.4): one owner and one effect per table.

Plan 03-06 owns the tests that read the whole schema; these prove the contract itself and what this plan registered.
"""

import json
from collections import Counter
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.trust import api, privacy

# What plan 03-01 registers, by table: (owning module, effect). Only tables that exist at the Phase 1 head plus the
# three this plan adds. The ingest tables belong to Phase 2.
EXPECTED = {
    "users": ("platform", "cascade"),
    "sessions": ("platform", "cascade"),
    "outbox": ("platform", "cascade"),
    "queue_jobs": ("platform", "cascade"),
    "idempotency_keys": ("platform", "cascade"),
    "login_codes": ("platform", "by_email"),
    "llm_calls": ("platform", "set_null"),
    "model_registry": ("platform", "no_personal_data"),
    "eval_items": ("evals", "cascade"),
    "eval_runs": ("evals", "no_personal_data"),
    "consents": ("trust", "cascade"),
    "audit_log": ("trust", "cascade"),
    "data_requests": ("trust", "kept"),
}


@pytest.fixture
def empty_registry(monkeypatch):
    """A registry of the test's own, put back afterwards."""
    monkeypatch.setattr(privacy, "_tables", {})
    monkeypatch.setattr(privacy, "_exporters", {})


def make_user(conn, email: str = "ada@example.test"):
    return conn.execute(text("INSERT INTO users (email) VALUES (:e) RETURNING id"), {"e": email}).scalar_one()


def test_registering_a_table_twice_is_refused_and_records_nothing(empty_registry):
    privacy.register("one", {"things": "cascade"})
    with pytest.raises(ValueError, match="things by one"):
        privacy.register("two", {"others": "kept", "things": "cascade"})
    assert privacy.registry() == {"one": {"things": "cascade"}}  # "others" was not recorded either


def test_a_module_cannot_register_one_of_its_own_tables_again(empty_registry):
    privacy.register("one", {"things": "cascade"})
    with pytest.raises(ValueError):
        privacy.register("one", {"things": "kept"})


@pytest.mark.parametrize("effect", privacy.EFFECTS)
def test_every_effect_is_accepted(empty_registry, effect):
    privacy.register("one", {"things": effect})
    assert privacy.registry() == {"one": {"things": effect}}


def test_an_unknown_effect_is_refused_naming_the_table(empty_registry):
    with pytest.raises(ValueError, match="things"):
        privacy.register("one", {"things": "delete"})
    assert privacy.registry() == {}


def test_registry_lists_every_table_with_its_effect_and_hands_out_copies(empty_registry):
    privacy.register("one", {"a": "cascade", "b": "set_null"})
    privacy.register("two", {"c": "no_personal_data"})
    listed = privacy.registry()
    assert listed == {"one": {"a": "cascade", "b": "set_null"}, "two": {"c": "no_personal_data"}}
    listed["one"]["a"] = "kept"
    listed["three"] = {}
    assert privacy.registry()["one"]["a"] == "cascade" and "three" not in privacy.registry()


def test_a_module_has_at_most_one_exporter(empty_registry):
    def export(conn, user_id):
        return privacy.ModuleExport(data={})

    privacy.register("one", {"a": "cascade"}, exporter=export)
    privacy.register("two", {"b": "no_personal_data"})
    assert privacy.exporters() == {"one": export}
    with pytest.raises(ValueError, match="already has an exporter"):
        privacy.register("one", {"c": "cascade"}, exporter=export)


def test_a_module_export_has_no_files_unless_it_adds_some():
    assert privacy.ModuleExport(data={"a": []}).files == {}


def test_the_platform_and_trust_tables_each_map_to_exactly_one_module_and_effect():
    owned = Counter(table for tables in privacy.registry().values() for table in tables)
    assert [table for table, count in owned.items() if count > 1] == []
    by_table = {
        table: (module, effect) for module, tables in privacy.registry().items() for table, effect in tables.items()
    }
    assert {table: by_table.get(table) for table in EXPECTED} == EXPECTED


def test_every_table_this_plan_registered_exists_in_the_schema(conn):
    existing = set(conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")).scalars())
    assert set(EXPECTED) <= existing


def test_the_ingest_tables_are_left_to_phase_2():
    registered = {table for tables in privacy.registry().values() for table in tables}
    assert {"job_sources", "jobs", "fetch_runs"}.isdisjoint(registered)


def test_the_trust_exporter_returns_the_persons_own_rows_as_json_and_never_the_archive(conn):
    ada, bob = make_user(conn), make_user(conn, "bob@example.test")
    api.grant_consent(conn, ada, "terms", "terms-v1")
    api.grant_consent(conn, bob, "terms", "terms-v1")
    api.audit_log(conn, "match.run_completed", "system", ada, {"shown": 2})
    api.audit_log(conn, "match.run_completed", "system", bob, {"shown": 9})
    conn.execute(
        text(
            """INSERT INTO data_requests (user_id, subject_user_id, kind, due_at, export_archive, store_results)
               VALUES (:u, :u, 'export', now() + interval '1 day', :archive, '{"profile": "exported"}')"""
        ),
        {"u": ada, "archive": b"SECRET-ARCHIVE-BYTES"},
    )
    conn.execute(
        text("INSERT INTO data_requests (user_id, subject_user_id, kind, due_at) VALUES (:u, :u, 'export', now())"),
        {"u": bob},
    )

    export = privacy.exporters()["trust"](conn, ada)

    assert isinstance(export, privacy.ModuleExport) and export.files == {}
    assert set(export.data) == {"consents", "audit_log", "data_requests"}
    assert [row["purpose"] for row in export.data["consents"]] == ["terms"]
    details = [row["details"] for row in export.data["audit_log"]]
    assert details == [{"purpose": "terms", "text_version": "terms-v1"}, {"shown": 2}]
    (request,) = export.data["data_requests"]
    assert request["kind"] == "export" and request["store_results"] == {"profile": "exported"}
    assert "export_archive" not in request
    text_form = json.dumps(export.data)  # JSON-ready as it stands
    assert str(bob) not in text_form and "SECRET-ARCHIVE-BYTES" not in text_form


def test_the_trust_exporter_returns_empty_lists_for_a_person_with_no_rows(conn):
    export = privacy.exporters()["trust"](conn, uuid4())
    assert export.data == {"consents": [], "audit_log": [], "data_requests": []}
