"""Shared fixtures.

All tests use in-memory SQLite or environment isolation — no live database,
no real platform calls, no secrets (AGENTS.md §5).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from publisher.db.models import Base

# Every variable from .env.example, so config tests never see host env values.
ENV_VARS = [
    "APP_ENV",
    "APP_HOST",
    "APP_PORT",
    "LOG_LEVEL",
    "DATA_DIR",
    "DATABASE_URL",
    "API_KEY",
    "SCHEDULER_ENABLED",
    "SCHEDULER_POLL_INTERVAL_SECONDS",
    "MAX_ATTEMPTS",
    "RETRY_BASE_DELAY_SECONDS",
    "RETRY_MAX_DELAY_SECONDS",
    "STALE_PUBLISHING_TIMEOUT_SECONDS",
    "TELEGRAM_BOT_TOKEN",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Remove all publisher-related environment variables."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.fixture
def sync_memory_engine() -> Iterator[Engine]:
    """A sync engine over a fresh in-memory SQLite with all tables created."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def sync_session(sync_memory_engine: Engine) -> Iterator[Session]:
    """A session bound to the fresh in-memory database."""
    session = Session(sync_memory_engine)
    try:
        yield session
    finally:
        session.close()
