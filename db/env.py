"""Alembic environment. Migrations run online, on SQLAlchemy 2 with psycopg 3.

The database URL comes from config.attributes["url"] (the test harness), else DATABASE_URL_MIGRATE, else the local
bootstrap login of the Compose database. The bootstrap login is a superuser: it creates roles and the vector extension.
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

DEFAULT_URL = "postgresql://app:app_local_only@127.0.0.1:5433/app"

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: do not silence the application's loggers when this runs inside a process.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

if context.is_offline_mode():
    raise RuntimeError("--sql is not supported: the migrations inspect the database as they run")

url = make_url(config.attributes.get("url") or os.environ.get("DATABASE_URL_MIGRATE") or DEFAULT_URL)
engine = create_engine(url.set(drivername="postgresql+psycopg"), poolclass=pool.NullPool)

with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
