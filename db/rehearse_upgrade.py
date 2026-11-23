"""Rehearse the Phase 2 upgrade on a restored copy of the live database, then compare what went in with what came out.

The live database, `app`, is only read and dumped here. It is never upgraded. The steps:

  1. count the rows of job_sources, jobs and fetch_runs in `app` (a read-only session)
  2. dump `app` with pg_dump (custom format) to a file outside the repository
  3. restore the dump into a new database whose name ends in _test
  4. run `alembic upgrade head` against that copy only, and time it
  5. check, job by job, that no row was lost and that every is_remote and employment_type value came out as the
     DATA_MODEL 8 rule says
  6. print a short report
  7. drop the copy, or keep it with --keep so the API and the browser can be tried over real data. A copy that failed
     a check is always kept, for inspection.

Run it from the repository root, with the backend environment, before the real upgrade:

    uv run --project backend python db/rehearse_upgrade.py [--keep]

It needs Docker (the database runs in the Compose container) and uses that database's development login.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import psycopg

REPO = Path(__file__).resolve().parents[1]
CONTAINER = "openelip-db-1"
HOST, PORT, USER, PASSWORD = "127.0.0.1", 5433, "app", "app_local_only"  # the Compose database's development login
LIVE_DATABASE = "app"
DEFAULT_COPY = "app_rehearsal_test"
DEFAULT_DUMP = REPO.parent / "openelip-backups" / "app-before-phase2.dump"  # beside the repository, never inside it

TABLES = ("job_sources", "jobs", "fetch_runs")
# DATA_MODEL 8: what an old is_remote value must read as in work_arrangements.
EXPECTED_ARRANGEMENTS: dict[bool | None, list[str]] = {True: ["remote"], False: ["onsite", "hybrid"], None: []}
SHOWN_IDS = 5  # a failing check names this many job ids, not thousands


def conninfo(database: str) -> str:
    return f"host={HOST} port={PORT} user={USER} password={PASSWORD} dbname={database}"


def require_test_name(name: str) -> None:
    """The rehearsal drops and upgrades its copy, so it refuses any name that could be a real database."""
    if name == LIVE_DATABASE or not re.fullmatch(r"[a-z0-9_]+_test", name):
        sys.exit(f"refusing to use database {name!r}: the rehearsal copy's name must end in _test")


def counts(conn: psycopg.Connection) -> dict[str, int]:
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in TABLES}


def recreate(name: str) -> None:
    require_test_name(name)
    with psycopg.connect(conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')


def drop(name: str) -> None:
    require_test_name(name)
    with psycopg.connect(conninfo("postgres"), autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def dump(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as out:
        subprocess.run(
            ["docker", "exec", CONTAINER, "pg_dump", "-U", USER, "-Fc", LIVE_DATABASE], stdout=out, check=True
        )


def restore(path: Path, name: str) -> None:
    """Owners and grants are restored too, so the copy behaves like `app` does."""
    require_test_name(name)
    with path.open("rb") as source:
        command = ["docker", "exec", "-i", CONTAINER, "pg_restore", "-U", USER, "-d", name, "--exit-on-error"]
        subprocess.run(command, stdin=source, check=True)


def upgrade(name: str) -> float:
    """alembic upgrade head against the copy only; returns the seconds it took."""
    require_test_name(name)
    url = f"postgresql://{USER}:{PASSWORD}@{HOST}:{PORT}/{name}"
    command = [sys.executable, "-m", "alembic", "-c", str(REPO / "backend" / "alembic.ini"), "upgrade", "head"]
    started = time.perf_counter()
    subprocess.run(command, cwd=REPO, env={**os.environ, "DATABASE_URL_MIGRATE": url}, check=True)
    return time.perf_counter() - started


def old_values(conn: psycopg.Connection) -> dict[int, tuple[bool | None, str | None]]:
    return {row[0]: (row[1], row[2]) for row in conn.execute("SELECT id, is_remote, employment_type FROM jobs")}


def new_values(conn: psycopg.Connection) -> dict[int, tuple[list[str], str | None, str]]:
    rows = conn.execute("SELECT id, work_arrangements::text[], employment_type_raw, employment_type::text FROM jobs")
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def problems_found(
    before: dict[str, int],
    after: dict[str, int],
    old: dict[int, tuple[bool | None, str | None]],
    new: dict[int, tuple[list[str], str | None, str]],
) -> list[str]:
    problems = []
    if before != after:
        problems.append(f"row counts changed: {before} -> {after}")
    kept = {i: (remote, wording) for i, (remote, wording) in old.items() if i in new}
    bad = {
        "a job is gone": [i for i in old if i not in new],
        "work_arrangements differs from the is_remote rule": [
            i for i, (remote, _) in kept.items() if new[i][0] != EXPECTED_ARRANGEMENTS[remote]
        ],
        "employment_type_raw differs from the old employment_type": [
            i for i, (_, wording) in kept.items() if new[i][1] != wording
        ],
    }
    for what, ids in bad.items():
        if ids:
            problems.append(f"{what}: {len(ids)} jobs, for example {ids[:SHOWN_IDS]}")
    return problems


def tally(values) -> str:
    return ", ".join(f"{key} {n}" for key, n in sorted(Counter(values).items(), key=lambda kv: str(kv[0])))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rehearse the Phase 2 upgrade on a restored copy of the live database."
    )
    parser.add_argument("--keep", action="store_true", help="keep the upgraded copy for the API and browser checks")
    parser.add_argument(
        "--database", default=DEFAULT_COPY, help=f"name of the copy, must end in _test ({DEFAULT_COPY})"
    )
    parser.add_argument("--dump", type=Path, default=DEFAULT_DUMP, help="where the dump of `app` is written")
    args = parser.parse_args()
    require_test_name(args.database)

    with psycopg.connect(conninfo(LIVE_DATABASE)) as live:
        live.read_only = True  # `app` is read, never written
        live_counts = counts(live)
    print(f"dumping {LIVE_DATABASE} to {args.dump} ...", flush=True)
    dump(args.dump)
    print(f"restoring into {args.database} ...", flush=True)
    recreate(args.database)
    restore(args.dump, args.database)
    with psycopg.connect(conninfo(args.database)) as copy:
        before, old = counts(copy), old_values(copy)
    print("running alembic upgrade head on the copy ...", flush=True)
    seconds = upgrade(args.database)
    with psycopg.connect(conninfo(args.database)) as copy:
        after, new = counts(copy), new_values(copy)
        version = copy.execute("SELECT version_num FROM alembic_version").fetchone()[0]

    problems = problems_found(before, after, old, new)
    print(f"\nRehearsal of the Phase 2 upgrade on {args.database} (now at {version})")
    print(
        f"  live {LIVE_DATABASE} now: {live_counts}" + ("" if live_counts == before else "  (changed since the dump)")
    )
    print(f"  copy before:  {before}\n  copy after:   {after}")
    print(f"  dump:         {args.dump} ({args.dump.stat().st_size / 1_000_000:.1f} MB)")
    print(f"  upgrade took: {seconds:.1f} s")
    print(f"  is_remote before:        {tally(remote for remote, _ in old.values())}")
    print(f"  work_arrangements after: {tally(tuple(a) for a, _, _ in new.values())}")
    print(f"  employment_type after:   {tally(kind for _, _, kind in new.values())}")
    print("  RESULT: " + ("pass" if not problems else "FAIL"))
    for problem in problems:
        print(f"    - {problem}")

    if problems or args.keep:
        print(f"  the copy {args.database} is kept")
    else:
        drop(args.database)
        print(f"  the copy {args.database} was dropped")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
