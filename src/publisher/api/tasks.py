"""`/api/v1/tasks` — task listing/detail (specification §7)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query

from ..db.models import TaskStatus
from ..db.repositories.tasks import get_task_with_attempts, query_tasks
from .deps import SessionDep, require_api_key
from .errors import ApiError, ErrorResponse
from .schemas import TaskDetailResponse, TaskListResponse, task_detail, task_view

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_api_key)])


@router.get(
    "",
    response_model=TaskListResponse,
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def list_tasks(
    session: SessionDep,
    status: TaskStatus | None = None,
    platform: str | None = None,
    account: str | None = None,
    post_type: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> TaskListResponse:
    """List publication tasks with filters and offset pagination.

    Filters: `status`, `platform`, `account`, `post_type`, and the bounded
    `created_from`/`created_to` range. `total` counts the full match set, so
    clients can page with `limit`/`offset`.
    """
    tasks, total = await query_tasks(
        session,
        status=status,
        platform=platform,
        account=account,
        post_type=post_type,
        created_from=created_from,
        created_to=created_to,
        limit=limit,
        offset=offset,
    )
    return TaskListResponse(
        items=[task_view(task) for task in tasks],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{task_id}",
    response_model=TaskDetailResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_task(
    task_id: str,
    session: SessionDep,
) -> TaskDetailResponse:
    """Retrieve one task with its post identity and attempt history."""
    task = await get_task_with_attempts(session, task_id)
    if task is None:
        raise ApiError(404, "not_found", "task not found")
    return task_detail(task)
