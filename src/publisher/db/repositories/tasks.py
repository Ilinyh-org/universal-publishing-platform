"""Read repositories for publication tasks."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..models import PublicationTask, TaskStatus


async def list_tasks(
    session: AsyncSession,
    *,
    status: TaskStatus | None = TaskStatus.PENDING,
    limit: int = 20,
) -> list[PublicationTask]:
    """List tasks oldest-first, optionally filtered by status.

    Post and account are eagerly loaded so callers can render rows without
    lazy IO in async contexts.
    """
    stmt = (
        select(PublicationTask)
        .options(
            joinedload(PublicationTask.post),
            joinedload(PublicationTask.account),
        )
        .order_by(PublicationTask.created_at, PublicationTask.id)
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(PublicationTask.status == status.value)
    result = await session.execute(stmt)
    return list(result.scalars().unique().all())
