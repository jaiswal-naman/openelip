"""Build the benchmark database from the frozen snapshot and the invented profiles (plan 04-07).

    cd backend
    uv run python ../evals/matching_benchmark/load.py --database-url postgresql://app:...@127.0.0.1:5433/<name>_test

It refuses any database whose name does not end in _test. It creates the database if it is missing, migrates it to head,
puts every snapshot posting in through the Phase 2 store path (ingest.fetch.store), embeds them with the Phase 2
embedding sweep (the local model, no key), and inserts each profile as a synthetic account with a confirmed record. A
profile's email is `<key>@example.test`, which is the key to user id map: the other scripts find a person by it. It
refuses a database that already holds postings, so a second load cannot half-overwrite the first.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import URL

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import target  # noqa: E402

SNAPSHOT = HERE / "snapshot.jsonl"
PROFILES = HERE / "profiles.jsonl"
# Record rows get explicit, increasing times, so the order of a record (and of the passage embedded from it) is fixed.
RECORD_EPOCH = datetime(2026, 11, 23, tzinfo=timezone.utc)

INSERT_SOURCE = text(
    "INSERT INTO job_sources (ats, token, company_name) VALUES (CAST(:ats AS ats_type), :token, :company) RETURNING id"
)
SET_FIRST_SEEN = text(
    "UPDATE jobs SET materially_changed_at = :changed WHERE source_id = :source AND external_id = :external_id"
)
INSERT_USER = text(
    "INSERT INTO users (email, display_name, timezone, is_synthetic) VALUES (:email, :name, :tz, true) RETURNING id"
)
INSERT_EVENT = text(
    """INSERT INTO profile_events (user_id, event_type, entity, operation, after, source, record_version_after,
                                  created_at)
       VALUES (:user, 'applied', CAST(:entity AS profile_entity), 'create', CAST(:after AS jsonb), 'candidate_edit',
               :version, :at) RETURNING id"""
)
INSERT_FACT = text(
    """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, profile_event_id, created_at,
                                 confirmed_at)
       VALUES (:user, CAST(:kind AS fact_kind), :statement, CAST(:attributes AS jsonb), 'candidate_edit', :event,
               :at, :at)"""
)
INSERT_CONSTRAINT = text(
    """INSERT INTO constraints (user_id, kind, statement, country_codes, cities, arrangements, employment_types,
                               seniorities, salary_floor, salary_currency, company_names, source, profile_event_id,
                               created_at)
       VALUES (:user, CAST(:kind AS constraint_kind), :statement, CAST(:country_codes AS char(2)[]),
               CAST(:cities AS text[]), CAST(:arrangements AS work_arrangement[]),
               CAST(:employment_types AS employment_type[]), CAST(:seniorities AS seniority[]), :salary_floor,
               :salary_currency, CAST(:company_names AS text[]), 'candidate_edit', :event, :at)"""
)
CONSTRAINT_COLUMNS = (
    "country_codes cities arrangements employment_types seniorities salary_floor salary_currency company_names".split()
)
INSERT_PREFERENCE = text(
    """INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id, created_at)
       VALUES (:user, CAST(:kind AS preference_kind), :value, :weight, :statement, 'candidate_edit', :event, :at)"""
)
CONFIRM_RECORD = text("UPDATE users SET record_version = :version, record_confirmed_at = :at WHERE id = :user")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def create_if_missing(url: URL) -> None:
    """CREATE DATABASE through the maintenance database, for a name target.require_scratch has already checked."""
    admin = create_engine(url.set(drivername="postgresql+psycopg", database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        if not conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}).scalar():
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    admin.dispose()


def migrate(url: URL) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(target.REPO / "db"))
    config.attributes["url"] = url  # read by db/env.py
    command.upgrade(config, "head")


def load_postings(conn: Connection, postings: list[dict[str, Any]]) -> int:
    """Every posting through ingest.fetch.store, one call per source as ingest makes them, then each one's first-seen
    time (the one column the store path cannot set). Returns how many postings the store path saw."""
    from app.ingest import fetch

    by_source: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for p in postings:
        by_source[(p["ats"], p["token"], p["company"])].append(
            {**p, "posted_at": datetime.fromisoformat(p["posted_at"]) if p["posted_at"] else None}
        )
    seen = 0
    for (ats, token, company), group in sorted(by_source.items()):
        source = conn.execute(INSERT_SOURCE, {"ats": ats, "token": token, "company": company}).scalar_one()
        seen += fetch.store(conn, source, company, group)["seen"]
        for p in group:
            conn.execute(
                SET_FIRST_SEEN,
                {"changed": datetime.fromisoformat(p["changed_at"]), "source": source, "external_id": p["external_id"]},
            )
    return seen


def embed_all(url: URL) -> int:
    """The Phase 2 embedding sweep until nothing is left to embed: title and the start of the description, because the
    snapshot has no extracted requirements. Raises EmbeddingUnavailable when the local model cannot be loaded."""
    from app.ingest import embedding
    from app.platform import db

    total = 0
    while True:
        with db.transaction(role="migrate") as conn:
            done = embedding.embed_pending(conn)
        total += done
        print(f"embedded {total}", flush=True)
        if not done:
            return total


def _insert_record(conn: Connection, user: UUID, profile: dict[str, Any]) -> int:
    """The profile's facts, constraints and preferences as a confirmed record. Each row points at its own change-log
    event, as the profile module writes them, and gets its own time, so the record keeps the order of the file."""
    version = 0

    def event(entity: str, after: dict[str, Any]) -> tuple[UUID, int, datetime]:
        nonlocal version
        version += 1
        at = RECORD_EPOCH + timedelta(seconds=version)
        row = {"user": user, "entity": entity, "after": json.dumps(after), "version": version, "at": at}
        return conn.execute(INSERT_EVENT, row).scalar_one(), version, at

    for fact in profile["facts"]:
        event_id, _, at = event("fact", {"kind": fact["kind"]})
        conn.execute(
            INSERT_FACT,
            {
                "user": user,
                "kind": fact["kind"],
                "statement": fact["statement"],
                "attributes": json.dumps(fact.get("attributes", {})),
                "event": event_id,
                "at": at,
            },
        )
    for constraint in profile["constraints"]:
        event_id, _, at = event("constraint", {"kind": constraint["kind"]})
        columns = {name: constraint.get(name) for name in CONSTRAINT_COLUMNS}
        conn.execute(
            INSERT_CONSTRAINT,
            {"user": user, "kind": constraint["kind"], "statement": constraint["statement"], **columns}
            | {"event": event_id, "at": at},
        )
    for preference in profile["preferences"]:
        event_id, _, at = event("preference", {"kind": preference["kind"]})
        conn.execute(
            INSERT_PREFERENCE,
            {
                "user": user,
                "kind": preference["kind"],
                "value": preference["value"],
                "weight": preference["weight"],
                "statement": preference["statement"],
                "event": event_id,
                "at": at,
            },
        )
    conn.execute(CONFIRM_RECORD, {"user": user, "version": version, "at": RECORD_EPOCH + timedelta(seconds=version)})
    return version


def load_profiles(conn: Connection, profiles: list[dict[str, Any]]) -> dict[str, UUID]:
    """Each profile as a synthetic account (`<key>@example.test`) with a confirmed record; returns key to user id."""
    ids = {}
    for profile in profiles:
        user = conn.execute(
            INSERT_USER, {"email": f"{profile['key']}@example.test", "name": profile["name"], "tz": profile["timezone"]}
        ).scalar_one()
        _insert_record(conn, user, profile)
        ids[profile["key"]] = user
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--database-url", required=True, help="a scratch database; its name must end in _test")
    args = ap.parse_args()
    url = target.require_scratch(args.database_url)
    create_if_missing(url)
    migrate(url)
    target.point_app_at(url)
    from app.platform import db

    with db.transaction(role="migrate") as conn:
        target.require_head(conn)
        if conn.execute(text("SELECT count(*) FROM jobs")).scalar_one():
            sys.exit(f"{url.database} already holds postings: drop it and load again")
        seen = load_postings(conn, read_jsonl(SNAPSHOT))
        ids = load_profiles(conn, read_jsonl(PROFILES))
    print(f"loaded {seen} postings and {len(ids)} profiles into {url.database}", flush=True)
    embed_all(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
