"""config/models.toml into the model_registry table (plan 01-04)."""

from sqlalchemy import Connection


def sync(conn: Connection) -> int:
    """Upsert one model_registry row per configured route and return how many."""
    raise NotImplementedError("plan 01-04")
