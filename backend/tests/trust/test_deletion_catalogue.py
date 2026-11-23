"""The deletion catalogue test (TRU-02; ARCHITECTURE 8.4, test 1; DATA_MODEL 7.2).

It reads the live schema (pg_constraint) and fails when erasure could leave a person behind: a foreign key to users
that is neither CASCADE nor SET NULL, a SET NULL that the data model does not list as kept on purpose, or a column
named like a user reference that is not a foreign key at all. Tables that do not exist yet are not listed here; the
checker looks at whatever the schema has, so a table a later phase adds is covered the day it exists.

Each rule has a self-test that builds the mistake in a table of its own (inside the test's transaction, which is rolled
back) and shows the checker reports it, so a checker that went blind would fail here instead of passing.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import text

REPO = Path(__file__).resolve().parents[3]
ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
# The one column named like a user reference that has no foreign key on purpose: the bare id kept after erasure.
KEPT_BY_DESIGN = {("data_requests", "subject_user_id")}

USER_FOREIGN_KEYS = """
    SELECT ns.nspname AS schema, cl.relname AS table_name, a.attname AS column_name, c.confdeltype AS action
    FROM pg_constraint c
    JOIN pg_class cl ON cl.oid = c.conrelid
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
    WHERE c.contype = 'f' AND c.confrelid = 'public.users'::regclass AND cardinality(c.conkey) = 1
"""
USER_COLUMNS = """
    SELECT ns.nspname AS schema, cl.relname AS table_name, a.attname AS column_name
    FROM pg_attribute a
    JOIN pg_class cl ON cl.oid = a.attrelid
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace
    WHERE cl.relkind IN ('r', 'p') AND ns.nspname IN ('public', 'langgraph') AND a.attnum > 0 AND NOT a.attisdropped
      AND (a.attname = 'user_id' OR right(a.attname, 8) = '_user_id')
"""
# DATA_MODEL.md 7.2: the checkpoints of a thread whose campaign or Twin question is gone must not outlive it.
ORPHAN_THREADS = """
    SELECT count(*) FROM (
      SELECT thread_id FROM langgraph.checkpoints
      UNION SELECT thread_id FROM langgraph.checkpoint_blobs
      UNION SELECT thread_id FROM langgraph.checkpoint_writes) t
    WHERE NOT (   (t.thread_id ~ '^campaign:[0-9a-f-]{36}$'
                   AND EXISTS (SELECT 1 FROM campaigns c WHERE 'campaign:' || c.id = t.thread_id))
               OR (t.thread_id ~ '^twin:[0-9a-f-]{36}$'
                   AND EXISTS (SELECT 1 FROM twin_messages m
                               WHERE m.author = 'employer' AND 'twin:' || m.id = t.thread_id)))
"""


def kept_on_purpose() -> set[tuple[str, str]]:
    """The references DATA_MODEL.md 7.2 says stay as NULL: read from the document, which is where they are decided."""
    document = (REPO / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")
    (row,) = [line for line in document.splitlines() if line.startswith("| Row kept, reference set to NULL |")]
    return set(re.findall(r"`([a-z_]+)\.([a-z_]+)`", row))


def named(schema: str, table: str) -> str:
    return table if schema == "public" else f"{schema}.{table}"


def catalogue_problems(conn, listed: set[tuple[str, str]]) -> list[str]:
    """Every way the schema could leave a person behind, each as a sentence naming the table and column."""
    problems = []
    keys = conn.execute(text(USER_FOREIGN_KEYS)).all()
    for key in keys:
        where = f"{named(key.schema, key.table_name)}.{key.column_name}"
        if key.action not in ("c", "n"):
            problems.append(
                f"{where} has a foreign key to users with {ACTIONS[key.action]}: it must be CASCADE or SET NULL"
            )
        elif key.action == "n" and (key.table_name, key.column_name) not in listed:
            problems.append(
                f"{where} is SET NULL but DATA_MODEL.md 7.2 does not list it as a reference kept on purpose: "
                "add it to that list, or make it CASCADE"
            )
    covered = {(key.schema, key.table_name, key.column_name) for key in keys}
    for column in conn.execute(text(USER_COLUMNS)).all():
        is_known = (column.schema, column.table_name, column.column_name) in covered
        if not is_known and (column.table_name, column.column_name) not in KEPT_BY_DESIGN:
            where = f"{named(column.schema, column.table_name)}.{column.column_name}"
            problems.append(f"{where} is named like a reference to a user but has no foreign key to users")
    return problems


def make_table(conn, name: str, column: str, definition: str) -> None:
    conn.execute(text(f"CREATE TABLE {name} (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), {column} {definition})"))


# ---- the real schema ----------------------------------------------------------------------------------


def test_the_list_of_kept_references_is_read_from_the_data_model():
    listed = kept_on_purpose()
    assert {("audit_log", "actor_user_id"), ("llm_calls", "user_id"), ("data_requests", "user_id")} <= listed
    assert len(listed) >= 10  # a parse that quietly found nothing would make the check below pass for no reason


def test_no_foreign_key_to_users_leaves_a_person_behind(conn):
    problems = catalogue_problems(conn, kept_on_purpose())
    assert not problems, "erasure could leave a person behind:\n  " + "\n  ".join(problems)


def test_every_foreign_key_to_users_in_this_schema_was_seen(conn):
    """The checker is not looking at an empty set: the tables Phases 1 and 3 made all reference users."""
    tables = {key.table_name for key in conn.execute(text(USER_FOREIGN_KEYS)).all()}
    assert {
        "sessions",
        "outbox",
        "queue_jobs",
        "idempotency_keys",
        "llm_calls",
        "eval_items",
        "consents",
        "audit_log",
    } <= tables
    assert {"data_requests", "documents", "profile_events", "profile_facts", "constraints", "preferences"} <= tables


# ---- the checker can fail -----------------------------------------------------------------------------


def test_a_user_id_column_with_no_foreign_key_is_reported(conn):
    make_table(conn, "catalogue_probe", "user_id", "uuid")
    problems = catalogue_problems(conn, kept_on_purpose())
    assert [p for p in problems if "catalogue_probe.user_id" in p and "no foreign key" in p]


@pytest.mark.parametrize("column", ["reviewer_user_id", "candidate_user_id", "user_id"])
def test_every_spelling_of_a_user_reference_is_caught(conn, column):
    make_table(conn, "catalogue_probe", column, "uuid")
    assert [p for p in catalogue_problems(conn, kept_on_purpose()) if f"catalogue_probe.{column}" in p]


def test_the_one_exception_is_data_requests_and_only_that_column(conn):
    make_table(conn, "catalogue_probe", "subject_user_id", "uuid")  # the same column name, in another table
    problems = catalogue_problems(conn, kept_on_purpose())
    assert [p for p in problems if "catalogue_probe.subject_user_id" in p]
    assert not [p for p in problems if "data_requests" in p]


@pytest.mark.parametrize("action", ["", "ON DELETE NO ACTION", "ON DELETE RESTRICT", "ON DELETE SET DEFAULT"])
def test_a_foreign_key_that_would_block_or_keep_the_user_is_reported(conn, action):
    make_table(conn, "catalogue_probe", "user_id", f"uuid REFERENCES users(id) {action}")
    problems = catalogue_problems(conn, kept_on_purpose())
    assert [p for p in problems if "catalogue_probe.user_id" in p and "must be CASCADE or SET NULL" in p]


def test_a_cascade_foreign_key_is_fine(conn):
    make_table(conn, "catalogue_probe", "user_id", "uuid REFERENCES users(id) ON DELETE CASCADE")
    assert catalogue_problems(conn, kept_on_purpose()) == []


def test_a_set_null_that_the_data_model_does_not_list_is_reported_and_one_it_lists_is_not(conn):
    make_table(conn, "catalogue_probe", "reviewer_user_id", "uuid REFERENCES users(id) ON DELETE SET NULL")
    assert [p for p in catalogue_problems(conn, kept_on_purpose()) if "catalogue_probe.reviewer_user_id" in p]
    listed = kept_on_purpose() | {("catalogue_probe", "reviewer_user_id")}  # the document lists it: accepted
    assert catalogue_problems(conn, listed) == []


def test_a_list_that_misses_a_real_set_null_makes_the_real_schema_fail(conn):
    """The list matters: with it empty, the SET NULL references the schema really has are all reported."""
    problems = catalogue_problems(conn, set())
    for where in ("audit_log.actor_user_id", "llm_calls.user_id", "data_requests.user_id"):
        assert [p for p in problems if where in p and "does not list it" in p], where


# ---- checkpoints of threads that are gone (DATA_MODEL 7.2; the langgraph schema arrives with campaigns) -


def make_checkpoint_schema(conn) -> None:
    for statement in (
        "CREATE SCHEMA langgraph",
        "CREATE TABLE langgraph.checkpoints (thread_id text)",
        "CREATE TABLE langgraph.checkpoint_blobs (thread_id text)",
        "CREATE TABLE langgraph.checkpoint_writes (thread_id text)",
        "CREATE TABLE campaigns (id uuid PRIMARY KEY)",
        "CREATE TABLE twin_messages (id uuid PRIMARY KEY, author text)",
    ):
        conn.execute(text(statement))


def test_no_checkpoint_outlives_its_campaign_or_question(conn):
    if conn.execute(text("SELECT to_regnamespace('langgraph') IS NULL")).scalar_one():
        pytest.skip("no langgraph schema yet: it arrives with campaigns, and this test then runs on its own")
    assert conn.execute(text(ORPHAN_THREADS)).scalar_one() == 0


def test_the_orphan_check_counts_a_thread_whose_owner_is_gone(conn):
    if conn.execute(text("SELECT to_regnamespace('langgraph') IS NOT NULL")).scalar_one():
        pytest.skip("the real langgraph schema exists; the test above covers it")
    make_checkpoint_schema(conn)
    alive, gone = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    conn.execute(text("INSERT INTO campaigns (id) VALUES (:c)"), {"c": alive})
    for thread in (f"campaign:{alive}", f"campaign:{gone}", "twin:not-a-uuid", f"twin:{gone}"):
        conn.execute(text("INSERT INTO langgraph.checkpoints (thread_id) VALUES (:t)"), {"t": thread})
    assert conn.execute(text(ORPHAN_THREADS)).scalar_one() == 3  # only the live campaign's thread is accepted
    conn.execute(text("DELETE FROM langgraph.checkpoints WHERE thread_id <> :t"), {"t": f"campaign:{alive}"})
    assert conn.execute(text(ORPHAN_THREADS)).scalar_one() == 0
