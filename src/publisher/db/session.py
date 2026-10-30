"""Async database engine and session helpers.

Asynchronous I/O for API and adapter network calls (spec §4). SQLite
foreign keys are enforced on every connection (spec §8).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


def create_engine(database_url: str) -> AsyncEngine:
    """Create an async engine for `database_url`.

    In-memory SQLite databases get a `StaticPool` so every session shares
    the same connection; without it each connection would see a different
    empty database.
    """
    kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite") and ":memory:" in database_url:
        kwargs["poolclass"] = StaticPool
    engine = create_async_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory with commit-safe defaults."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Run a unit of work: commit on success, rollback on any error."""
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
