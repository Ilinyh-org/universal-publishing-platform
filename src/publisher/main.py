"""FastAPI application entry point (roadmap step 3).

Run locally:

    uvicorn publisher.main:app --host 0.0.0.0 --port 8000

Startup validates production settings (`Settings.validate_production`),
builds the async engine/session factory from `DATABASE_URL`, and mounts the
`/health` plus `/api/v1` routers. Database migrations are a deployment step
(`alembic upgrade head`), not an application startup step (specification §15).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from . import __version__
from .api import health, posts, tasks
from .api.errors import register_error_handlers
from .core.config import Settings
from .db.session import create_engine, make_session_factory

_DESCRIPTION = (
    "Self-hosted publishing service: batch ingestion of universal posts, "
    "per-target publication tasks, and platform delivery through adapters. "
    "All endpoints except /health require the X-API-Key header."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate settings, wire the database, dispose on shutdown."""
    settings: Settings = app.state.settings
    # Full production validation before serving (roadmap step 3, spec §14).
    settings.validate_production()
    engine: AsyncEngine | None = getattr(app.state, "engine", None)
    owns_engine = engine is None
    if engine is None:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    try:
        yield
    finally:
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
