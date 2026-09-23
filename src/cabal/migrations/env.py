"""Alembic env.

Reads DB URL from `CABAL_DB_URL` env var (or `<state_root>/data/cabal.db`, see cabal.config.home), uses
the SQLAlchemy metadata from `cabal.core.models.Base`, and runs migrations in
either offline (--sql) or online mode.

Online mode uses the sync SQLite driver (`sqlite:///`) — Alembic's run_migrations
loop is synchronous, and async drivers don't add anything for schema DDL.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from cabal.core.models import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    """Pick the migration URL.

    Order: explicit override in alembic.ini > CABAL_DB_URL env > default file.
    Always returned as a *sync* SQLite URL (sqlite:///) because Alembic's
    runtime is sync; the app's runtime uses sqlite+aiosqlite separately.
    """
    explicit = config.get_main_option("sqlalchemy.url")
    if explicit:
        return explicit
    from cabal.core.db import sync_db_url

    return sync_db_url()


target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
