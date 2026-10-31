"""Shared FastAPI dependencies: settings, database session, API key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.config import Settings
from ..core.security import ApiKeyError, verify_api_key
from .errors import ApiError


def get_settings(request: Request) -> Settings:
    """Application settings from app state (set by `create_app`)."""
    settings: Settings = request.app.state.settings
    return settings


def get_session_factory(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> async_sessionmaker[AsyncSession]:
    """Session factory built at startup from the configured database URL."""
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "session_factory", None
    )
    if factory is None:  # pragma: no cover - lifespan always sets it
        raise ApiError(500, "internal_error", "database session factory is not initialized")
    return factory


async def get_session(
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[AsyncSession]:
    """One request-scoped database session."""
    async with factory() as session:
        yield session


async def require_api_key(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """`X-API-Key` authentication for everything except `/health` (spec §13)."""
    try:
        verify_api_key(request.headers.get("X-API-Key"), settings.api_key)
    except ApiKeyError as exc:
        raise ApiError(401, "unauthorized", exc.message) from exc


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
