"""Async-capable Alembic environment (specification §4, §8).

The migration target is `publisher.db.models.Base.metadata`, so the schema
in migrations always mirrors the SQLAlchemy models. The database URL comes
from the `DATABASE_URL` environment variable (spec §14) and falls back to
`Settings().database_url`; both async (`sqlite+aiosqlite://...`) and sync
(`sqlite://...`) URLs are supported, so `alembic upgrade head` works from a
shell and from tests.

`render_as_batch=True` keeps future SQLite schema changes possible (ALTER
TABLE is emulated with batch copies on SQLite).
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from publisher.core.config import Settings
from publisher.db.models import Base

config = context.config

if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Environment wins; packaged default is the fallback (spec §14)."""
    override = os.getenv("DATABASE_URL")
    if override:
        return override
    return Settings().database_url


def _configure(context_config: dict[str, object]) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        **context_config,
    )


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (`alembic upgrade head --sql`)."""
    _configure(
        {
            "url": _database_url(),
            "literal_binds": True,
            "dialect_opts": {"paramstyle": "named"},
        }
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    _configure({"connection": connection})
    with context.begin_transaction():
        context.run_migrations()


def _is_async_url(url: str) -> bool:
    return "+aiosqlite" in url


def _run_async_migrations(url: str) -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = async_engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    async def migrate() -> None:
        async with connectable.connect() as connection:
            await connection.run_sync(_run_migrations)
        await connectable.dispose()

    asyncio.run(migrate())


def _run_sync_migrations(url: str) -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    url = _database_url()
    if _is_async_url(url):
        _run_async_migrations(url)
    else:
        _run_sync_migrations(url)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
