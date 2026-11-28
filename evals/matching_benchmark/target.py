"""Which database the benchmark scripts may touch (plan 04-07, threat T-04-37).

Every script of this folder acts on a scratch database only: a name ending in _test, as every test database has. The
application's own settings default to the live database `app` for all four roles, so point_app_at() replaces all four
before anything connects, and nothing can fall back to the default. The scripts import this module by plain name (each
puts its own folder on the import path first).
"""

import os
import re
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.engine import URL, make_url

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))  # job_refs() imports export_snapshot by plain name, like every script of this folder
SCRATCH = re.compile(r"[a-z0-9_]+_test")
# The login each application role connects with, its password variable and its development default (as the test
# harness builds them); the migration login is the one in the URL given.
LOGINS = {
    "API": ("app_api", "APP_DB_API_PASSWORD", "app_api_local_only"),
    "WORKER": ("app_worker", "APP_DB_WORKER_PASSWORD", "app_worker_local_only"),
    "MATCH": ("app_match", "APP_DB_MATCH_PASSWORD", "app_match_local_only"),
}


def require_scratch(url_text: str) -> URL:
    """The URL, or an exit when the database is not a scratch one."""
    url = make_url(url_text)
    if not SCRATCH.fullmatch(url.database or ""):
        sys.exit(f"refusing to use database {url.database!r}: a benchmark database name must end in _test")
    return url


def point_app_at(url: URL) -> None:
    """Make the application's four database roles (migrate, api, worker, match) connect to this database."""
    from app.platform import config

    os.environ["DATABASE_URL_MIGRATE"] = url.render_as_string(hide_password=False)
    # The benchmark's vector methods are measured with the local model (REPORT.md), and its invented profiles must
    # not be sent to a provider by a key left in the shell. embedding_compare.py is the OpenAI comparison.
    os.environ["EMBEDDING_PROVIDER"] = "local"
    for name, (login, variable, default) in LOGINS.items():
        login_url = url.set(username=login, password=os.environ.get(variable) or default)
        os.environ[f"DATABASE_URL_{name}"] = login_url.render_as_string(hide_password=False)
    config.get.cache_clear()


def head_revision() -> str:
    """The head of the migration history in this checkout (Alembic's own reading of db/)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(REPO / "db"))
    return str(ScriptDirectory.from_config(config).get_current_head())


def require_head(conn: Connection) -> None:
    """Exit unless the database is at the head of this checkout's migrations."""
    revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if revision != head_revision():
        sys.exit(f"the database is at {revision}, not at {head_revision()}: run load.py, which migrates it")


def job_refs(conn: Connection) -> dict[int, str]:
    """Database job id to the stable ref of the posting (read as the migration login: the match role cannot read
    job_sources)."""
    from export_snapshot import ref_of

    rows = conn.execute(
        text(
            "SELECT j.id, s.ats::text AS ats, s.token, j.external_id "
            "FROM jobs j JOIN job_sources s ON s.id = j.source_id"
        )
    )
    return {row.id: ref_of(row.ats, row.token, row.external_id) for row in rows}


def profile_ids(conn: Connection) -> dict[str, UUID]:
    """Profile key to user id: the benchmark's accounts are <key>@example.test (load.py)."""
    rows = conn.execute(
        text(
            "SELECT split_part(email, '@', 1) AS key, id FROM users WHERE is_synthetic AND email LIKE '%@example.test'"
        )
    )
    return {row.key: row.id for row in rows}
