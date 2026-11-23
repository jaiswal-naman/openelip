"""Which database a scored-by-script eval suite may write to.

The location and requirement suites record their runs from a script, as the worker role, into the database
DATABASE_URL_WORKER names, and its default is the live one. A finished run is frozen for good (eval_runs and eval_items
refuse changes), so a run written to the live database by accident stays there. A script therefore goes on only when
that database is a scratch one: its name ends in _test, as every test database does, or is exactly `evals`, the database
of the CI eval job (.github/workflows/ci.yml). `--live` says the person running it means to write to another one.

Only DATABASE_URL_WORKER is read: the scripts connect as the worker role and never as the migration login.
"""

import os
import re

from sqlalchemy.engine import make_url

SCRATCH_NAME = re.compile(r"[a-z0-9_]+_test|evals")


def refusal(live: bool) -> str | None:
    """Why a script that writes eval rows may not go on, or None when it may."""
    url = os.environ.get("DATABASE_URL_WORKER")
    if not url:
        return "DATABASE_URL_WORKER is not set and its default is the live database: name the database to write to"
    name = make_url(url).database or ""
    if live or SCRATCH_NAME.fullmatch(name):
        return None
    return (
        f"DATABASE_URL_WORKER names database {name!r}, which is not a scratch one (a name ending in _test, or "
        "'evals'): a recorded run cannot be deleted. Add --live to write to it anyway"
    )
