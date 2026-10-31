"""Read repositories for publication tasks."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, joinedload, selectinload

from ..models import Account, Platform, Post, PublicationTask, TaskStatus


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


async def query_tasks(
    session: AsyncSession,
    *,
    status: TaskStatus | None = None,
    platform: str | None = None,
    account: str | None = None,
    post_type: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[PublicationTask], int]:
    """Filtered, paginated task listing with the total match count.

    Filters (specification §7): status, platform key, account key, post type,
    and a bounded `created_at` range. `contains_eager` populates the related
    post/account/platform from the filter joins, so no extra queries run when
    rendering rows.
    """
    filters = []
    if status is not None:
        filters.append(PublicationTask.status == status.value)
    if platform is not None:
        filters.append(Platform.key == platform)
    if account is not None:
        filters.append(Account.key == account)
    if post_type is not None:
        filters.append(Post.post_type == post_type)
    if created_from is not None:
        filters.append(PublicationTask.created_at >= created_from)
    if created_to is not None:
        filters.append(PublicationTask.created_at <= created_to)

    base = (
        select(PublicationTask)
        .join(PublicationTask.account)
        .join(Account.platform)
        .join(PublicationTask.post)
        .where(*filters)
    )
    total = (
        await session.execute(
            select(func.count())
            .select_from(PublicationTask)
            .join(PublicationTask.account)
            .join(Account.platform)
            .join(PublicationTask.post)
            .where(*filters)
        )
    ).scalar_one()

    rows = await session.execute(
        base.options(
            contains_eager(PublicationTask.account).contains_eager(Account.platform),
            contains_eager(PublicationTask.post),
        )
        .order_by(PublicationTask.created_at, PublicationTask.id)
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().unique().all()), total


async def get_task_with_attempts(
    session: AsyncSession, task_id: str
) -> PublicationTask | None:
    """Load one task with its post, account identity, and attempt history."""
    stmt = (
        select(PublicationTask)
        .options(
            joinedload(PublicationTask.post),
            joinedload(PublicationTask.account).joinedload(Account.platform),
            selectinload(PublicationTask.attempts),
        )
        .where(PublicationTask.id == task_id)
    )
    return (await session.execute(stmt)).unique().scalar_one_or_none()
