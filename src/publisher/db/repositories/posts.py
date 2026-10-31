"""Read repositories for posts."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from ..models import Account, Post, PublicationTask


async def get_post_with_tasks(session: AsyncSession, post_id: str) -> Post | None:
    """Load one post with its tasks and account/platform identity eagerly.

    Eager loading keeps async rendering lazy-IO free (the task list is small:
    one row per target account).
    """
    stmt = (
        select(Post)
        .options(
            joinedload(Post.tasks).joinedload(PublicationTask.account).joinedload(Account.platform)
        )
        .where(Post.id == post_id)
    )
    return (await session.execute(stmt)).unique().scalar_one_or_none()
