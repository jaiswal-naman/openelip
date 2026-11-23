"""Phase 2's job index and Phase 3's career record, joined into one Alembic history.

Both phases were built from 0002_platform. They are one straight line (job index, trust, record, skill keys), and the
types both of them need are created once with DATA_MODEL's values and dropped once.
"""

import re
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

REPO = Path(__file__).resolve().parents[3]
PHASE_1_HEAD = "0002_platform"
# The job index has columns of these types and the record's constraints table has arrays of them.
SHARED_TYPES = ("work_arrangement", "employment_type", "seniority")


def _engine(url: URL) -> Engine:
    return create_engine(url.set(drivername="postgresql+psycopg"))


def _data_model_labels(type_name: str) -> list[str]:
    source = (REPO / "docs" / "DATA_MODEL.md").read_text(encoding="utf-8")
    match = re.search(rf"CREATE TYPE\s+{type_name}\s+AS ENUM\s*\(([^)]*)\)", source)
    assert match, f"DATA_MODEL.md has no CREATE TYPE {type_name}"
    return re.findall(r"'([^']+)'", match.group(1))


def _enum_types(engine: Engine) -> dict[str, list[str]]:
    """Every enum type in the public schema with its labels in order."""
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT t.typname, array_agg(e.enumlabel::text ORDER BY e.enumsortorder) FROM pg_type t "
                "JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' GROUP BY t.typname"
            )
        )
        return {name: labels for name, labels in rows}


def test_there_is_one_head_and_the_record_sits_on_top_of_the_job_index():
    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    ancestors = {r.revision for r in script.iterate_revisions("p3_0302_record", "base")}
    assert "p2_0201_job_index" in ancestors  # so the record is always downgraded before the job index


@pytest.mark.parametrize("type_name", SHARED_TYPES)
def test_the_shared_types_hold_the_data_model_values(engine, type_name):
    assert _enum_types(engine)[type_name] == _data_model_labels(type_name)


def test_the_skill_keys_point_at_skills(engine):
    with engine.connect() as c:
        keys = c.execute(
            text(
                "SELECT conrelid::regclass::text, confrelid::regclass::text, convalidated FROM pg_constraint "
                "WHERE contype = 'f' AND conname IN ('profile_facts_skill_id_fkey', 'preferences_skill_id_fkey')"
            )
        ).all()
    assert sorted(keys) == [("preferences", "skills", True), ("profile_facts", "skills", True)]


FACT = (
    "INSERT INTO profile_facts (user_id, kind, statement, source, profile_event_id, skill_id) "
    "VALUES (:u, 'skill', 'Python', 'candidate_edit', :e, :skill)"
)
PREFERENCE = (
    "INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id, skill_id) "
    "VALUES (:u, 'target_skill', 'python', 1, 'Python', 'candidate_edit', :e, :skill)"
)


@pytest.mark.parametrize("insert", [FACT, PREFERENCE], ids=["profile_facts", "preferences"])
def test_a_skill_id_must_name_a_skill_that_exists(conn, insert):
    conn.execute(text("INSERT INTO skills (id, external_id, name, taxonomy_version) VALUES (1, 'x-1', 'Python', 't')"))
    user = conn.execute(text("INSERT INTO users (email) VALUES ('skill-key@example.test') RETURNING id")).scalar_one()
    event = conn.execute(
        text(
            "INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after) "
            "VALUES (:u, 'applied', 'fact', 'create', '{}'::jsonb, 'candidate_edit', 1) RETURNING id"
        ),
        {"u": user},
    ).scalar_one()
    conn.execute(text(insert), {"u": user, "e": event, "skill": 1})  # a known skill is accepted
    with pytest.raises(IntegrityError, match="skill_id_fkey"):
        conn.execute(text(insert), {"u": user, "e": event, "skill": 999999})


def test_going_down_to_phase_1_drops_every_type_the_two_phases_made_and_up_again_makes_them(make_database, alembic_run):
    url = make_database("merged_types_test")
    alembic_run("upgrade", url, PHASE_1_HEAD)
    engine = _engine(url)
    phase_1_types = set(_enum_types(engine))
    alembic_run("upgrade", url, "head")
    at_head = _enum_types(engine)
    assert set(SHARED_TYPES) | {"consent_purpose", "fact_kind", "requirement_kind"} <= set(at_head)

    alembic_run("downgrade", url, PHASE_1_HEAD)  # each shared type is dropped by the job index's downgrade, once
    assert set(_enum_types(engine)) == phase_1_types

    alembic_run("upgrade", url, "head")
    assert _enum_types(engine).keys() == at_head.keys()
