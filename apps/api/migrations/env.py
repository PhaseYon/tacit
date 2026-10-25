"""Alembic runs as tacit_migrator, which owns every table (including Better Auth's)."""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from tacit_api.config import get_settings
from tacit_api.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Tests point this at a throwaway database.
DB_URL = os.environ.get("ALEMBIC_DATABASE_URL") or get_settings().migrator_database_url


def run_migrations_offline() -> None:
    context.configure(url=DB_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(DB_URL, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
