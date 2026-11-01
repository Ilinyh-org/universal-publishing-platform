"""FastAPI application entry point (roadmap step 3; startup wiring step 5).

Run locally:

    uvicorn publisher.main:app --host 0.0.0.0 --port 8000

Startup validates production settings (`Settings.validate_production`),
builds the async engine/session factory from `DATABASE_URL`, and mounts the
`/health` plus `/api/v1` routers. When `ENGINE_ENABLED` is true (the
production default) the lifespan also starts the background services — the
APScheduler schedule-assignment pass and the publishing engine — and stops
them gracefully on shutdown. Tests and CLI runs set `ENGINE_ENABLED=false`
so behavior stays deterministic. Database migrations are a deployment step
(`alembic upgrade head`), not an application startup step (specification §15).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from . import __version__
from .adapters.registry import AdapterRegistry
from .adapters.telegram import TelegramAdapter
from .api import health, posts, tasks
from .api.errors import register_error_handlers
from .core.config import Settings
from .core.publishing import EngineConfig, PublishingEngineService
from .core.scheduling import SchedulerService
from .core.secrets import EnvSecretProvider
from .db.session import create_engine, make_session_factory

_DESCRIPTION = (
    "Self-hosted publishing service: batch ingestion of universal posts, "
    "per-target publication tasks, and platform delivery through adapters. "
    "All endpoints except /health require the X-API-Key header."
)


def build_default_registry() -> AdapterRegistry:
    """Composition root: the adapters serving this deployment (spec §11.3).

    Platform-aware wiring lives here, never in core modules.
    """
    registry = AdapterRegistry()
    registry.register(TelegramAdapter())
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate settings, wire the database, start/stop background services."""
    settings: Settings = app.state.settings
    # Full production validation before serving (roadmap step 3, spec §14).
    settings.validate_production()
    engine: AsyncEngine | None = getattr(app.state, "engine", None)
    owns_engine = engine is None
    if engine is None:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    scheduler_service: SchedulerService | None = None
    publishing_service: PublishingEngineService | None = None
    if settings.engine_enabled:
        # Background work (roadmap step 5): schedule assignment plus the
        # publishing engine, both on the shared poll interval. Disabled by
        # configuration in tests/CLI so behavior stays deterministic.
        scheduler_service = SchedulerService(
            app.state.session_factory,
            interval_seconds=settings.scheduler_poll_interval_seconds,
            enabled=settings.scheduler_enabled,
        )
        publishing_service = PublishingEngineService(
            app.state.session_factory,
            build_default_registry(),
            EnvSecretProvider(),
            EngineConfig.from_settings(settings),
            interval_seconds=settings.scheduler_poll_interval_seconds,
        )
        scheduler_service.start()
        publishing_service.start()
        app.state.scheduler_service = scheduler_service
        app.state.publishing_service = publishing_service
    try:
        yield
    finally:
        if publishing_service is not None:
            await publishing_service.shutdown()
        if scheduler_service is not None:
            await scheduler_service.shutdown()
        app.state.session_factory = None
        if owns_engine:
            await engine.dispose()


def create_app(
    settings: Settings | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build the application.

    `settings`/`engine` are injectable for tests; production uses the
    environment via `Settings()`. A pre-set engine (tests, embedding) is
    used as-is and not disposed by the lifespan.
    """
    app = FastAPI(
        title="Universal Publishing Platform",
        description=_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings or Settings()
    if engine is not None:
        app.state.engine = engine

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(posts.router, prefix="/api/v1")
    app.include_router(tasks.router, prefix="/api/v1")
    return app


app = create_app()
