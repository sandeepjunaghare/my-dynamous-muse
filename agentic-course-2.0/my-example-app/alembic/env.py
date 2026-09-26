"""Alembic migration environment — async, and configured from the environment.

Two deliberate departures from what ``alembic init`` writes:

* The connection URL comes from ``app.core.config``, not from ``alembic.ini``. Config is strict
  12-factor, and a URL committed to an ini file is a credential waiting to leak.
* Migrations run against an async engine, matching the application. The default (non-``-t async``)
  template is sync and fails outright against ``postgresql+asyncpg://``.

Like the application, Alembic should point at Supabase's **direct** port (5432), not the pooler —
see ``.env.example``.
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.core.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares against this. T1's baseline defines no domain tables; T2 and T4 add theirs.
target_metadata = Base.metadata


def get_url() -> str:
    """Return the database URL from application settings."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit migrations as SQL, without needing a live connection."""
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations against an already-established sync-facing connection."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run the migrations through a shared connection."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
