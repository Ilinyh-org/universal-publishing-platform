"""Persistence models (specification §8).

Opaque generated IDs (UUID4 strings), UTC timestamps, and database-level
uniqueness constraints. Raw credentials are never stored: accounts carry a
`credentials_ref` pointing at an environment variable or mounted secret.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all persistence models."""


class TaskStatus(StrEnum):
    """`publication_tasks` lifecycle (specification §8.5)."""

    PENDING = "pending"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    RETRY_WAIT = "retry_wait"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TASK_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in TaskStatus)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return str(uuid.uuid4())


class Platform(Base):
    """A registered adapter type, not an individual account (spec §8.2)."""

    __tablename__ = "platforms"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    key: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    accounts: Mapped[list[Account]] = relationship(back_populates="platform")


class Account(Base):
    """A configured destination account (spec §8.3)."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("platform_id", "key", name="uq_accounts_platform_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    platform_id: Mapped[str] = mapped_column(ForeignKey("platforms.id"))
    key: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    credentials_ref: Mapped[str] = mapped_column(String(255))
    configuration_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    platform: Mapped[Platform] = relationship(back_populates="accounts")
    schedules: Mapped[list[PublishingSchedule]] = relationship(back_populates="account")
    tasks: Mapped[list[PublicationTask]] = relationship(back_populates="account")


class Post(Base):
    """Original accepted universal content (spec §8.1)."""

    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_posts_source_external_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    source: Mapped[str] = mapped_column(String(255))
    external_id: Mapped[str] = mapped_column(String(255))
    content_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    post_type: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    tasks: Mapped[list[PublicationTask]] = relationship(back_populates="post")


class PublishingSchedule(Base):
    """When an account may publish a post type (spec §8.4)."""

    __tablename__ = "publishing_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    post_type: Mapped[str] = mapped_column(String(64))
    publish_time: Mapped[time] = mapped_column(Time)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    days_of_week: Mapped[list[int]] = mapped_column(JSON, default=lambda: [1, 2, 3, 4, 5, 6, 7])
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    account: Mapped[Account] = relationship(back_populates="schedules")


class PublicationTask(Base):
    """The operational unit of publishing: one (post, account) pair (spec §8.5)."""

    __tablename__ = "publication_tasks"
    __table_args__ = (
        UniqueConstraint("post_id", "account_id", name="uq_publication_tasks_post_account"),
        # Slot exclusivity (spec §9.2): at most one task per account slot.
        # SQL NULL semantics leave unscheduled rows (scheduled_at IS NULL)
        # unaffected.
        Index(
            "uq_publication_tasks_account_slot",
            "account_id",
            "scheduled_at",
            unique=True,
        ),
        CheckConstraint(
            f"status IN ({_TASK_STATUS_VALUES})", name="ck_publication_tasks_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"))
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.PENDING.value)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_post_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    post: Mapped[Post] = relationship(back_populates="tasks")
    account: Mapped[Account] = relationship(back_populates="tasks")
    attempts: Mapped[list[PublicationAttempt]] = relationship(back_populates="task")


class PublicationAttempt(Base):
    """Append-only history of actual publish attempts (spec §8.6)."""

    __tablename__ = "publication_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "attempt_number", name="uq_publication_attempts_task_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("publication_tasks.id"))
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success: Mapped[bool | None] = mapped_column(nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

    task: Mapped[PublicationTask] = relationship(back_populates="attempts")
