"""Response schemas and ORM-to-view mappers (specification §7).

Views are built explicitly from ORM objects: field names stay API-stable,
and secret-adjacent columns (`credentials_ref`, configuration) are never
mapped, so no response can leak them (spec §13).
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator

from ..db.models import Account, Post, PublicationAttempt, PublicationTask


def _ensure_utc(value: Any) -> Any:
    """SQLite returns naive datetimes; the storage convention is UTC."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    if isinstance(value, time):
        return value
    return value


UtcDateTime = Annotated[datetime, BeforeValidator(_ensure_utc)]


class HealthResponse(BaseModel):
    """Liveness response — deliberately minimal, no configuration (spec §16)."""

    status: str


class AccountView(BaseModel):
    """Account identity in responses — no credentials or configuration."""

    id: str
    platform: str
    key: str
    display_name: str


class TaskView(BaseModel):
    """One publication task with its target account."""

    id: str
    post_id: str
    status: str
    scheduled_at: UtcDateTime | None = None
    next_attempt_at: UtcDateTime | None = None
    published_at: UtcDateTime | None = None
    external_post_id: str | None = None
    attempt_count: int
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
    account: AccountView


class PostBrief(BaseModel):
    """Post identity attached to task views."""

    id: str
    source: str
    external_id: str
    post_type: str


class PostDetailResponse(BaseModel):
    """A post plus its per-target publication tasks (specification §7)."""

    id: str
    source: str
    external_id: str
    content: dict[str, Any]
    post_type: str
    metadata: dict[str, Any] | None = None
    source_created_at: UtcDateTime | None = None
    received_at: UtcDateTime
    created_at: UtcDateTime
    tasks: list[TaskView]


class AttemptView(BaseModel):
    """One recorded publish attempt (sanitized fields only, spec §8.6)."""

    id: str
    attempt_number: int
    started_at: UtcDateTime
    finished_at: UtcDateTime | None = None
    success: bool | None = None
    error_code: str | None = None
    error_message: str | None = None
    response_metadata: dict[str, Any] | None = None


class TaskDetailResponse(TaskView):
    """A task plus its post identity and attempt history (specification §7)."""

    post: PostBrief
    attempts: list[AttemptView]


class TaskListResponse(BaseModel):
    """Filtered, paginated task listing."""

    items: list[TaskView]
    total: int
    limit: int
    offset: int


def account_view(account: Account) -> AccountView:
    """Identity-only account view; credentials/configuration never mapped."""
    return AccountView(
        id=account.id,
        platform=account.platform.key,
        key=account.key,
        display_name=account.display_name,
    )


def task_view(task: PublicationTask) -> TaskView:
    return TaskView(
        id=task.id,
        post_id=task.post_id,
        status=task.status,
        scheduled_at=task.scheduled_at,
        next_attempt_at=task.next_attempt_at,
        published_at=task.published_at,
        external_post_id=task.external_post_id,
        attempt_count=task.attempt_count,
        last_error_code=task.last_error_code,
        last_error_message=task.last_error_message,
        created_at=task.created_at,
        updated_at=task.updated_at,
        account=account_view(task.account),
    )


def post_detail(post: Post) -> PostDetailResponse:
    return PostDetailResponse(
        id=post.id,
        source=post.source,
        external_id=post.external_id,
        content=post.content_json,
        post_type=post.post_type,
        metadata=post.metadata_json,
        source_created_at=post.source_created_at,
        received_at=post.received_at,
        created_at=post.created_at,
        tasks=[task_view(task) for task in post.tasks],
    )


def attempt_view(attempt: PublicationAttempt) -> AttemptView:
    return AttemptView(
        id=attempt.id,
        attempt_number=attempt.attempt_number,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        success=attempt.success,
        error_code=attempt.error_code,
        error_message=attempt.error_message,
        response_metadata=attempt.response_metadata_json,
    )


def task_detail(task: PublicationTask) -> TaskDetailResponse:
    return TaskDetailResponse(
        **task_view(task).model_dump(),
        post=PostBrief(
            id=task.post.id,
            source=task.post.source,
            external_id=task.post.external_id,
            post_type=task.post.post_type,
        ),
        attempts=[attempt_view(a) for a in sorted(task.attempts, key=lambda a: a.attempt_number)],
    )
