"""Persistence model tests: constraints, defaults, SQLite FK enforcement."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from publisher.db.models import (
    Account,
    Base,
    Platform,
    Post,
    PublicationAttempt,
    PublicationTask,
    PublishingSchedule,
    TaskStatus,
)
from publisher.db.session import create_engine, make_session_factory


def _make_world(session: Session) -> tuple[Post, Account]:
    telegram = Platform(key="telegram", display_name="Telegram")
    account = Account(
        platform=telegram,
        key="english_channel",
        display_name="English A1",
        credentials_ref="TELEGRAM_BOT_TOKEN",
        configuration_json={"chat_id": "set-at-deployment"},
    )
    post = Post(
        source="n8n",
        external_id="e-1",
        content_json={"type": "text", "body": "hi"},
        post_type="dialogue",
    )
    session.add_all([telegram, account, post])
    session.flush()
    return post, account


def test_post_unique_source_external_id(sync_session: Session) -> None:
    post, _ = _make_world(sync_session)
    sync_session.add(
        Post(
            source=post.source,
            external_id=post.external_id,
            content_json={"type": "text", "body": "other"},
            post_type="grammar",
        )
    )
    with pytest.raises(IntegrityError):
        sync_session.flush()


def test_account_unique_within_platform(sync_session: Session) -> None:
    _, account = _make_world(sync_session)
    duplicate = Account(
        platform_id=account.platform_id,
        key=account.key,
        display_name="Duplicate",
        credentials_ref="TELEGRAM_BOT_TOKEN",
    )
    sync_session.add(duplicate)
    with pytest.raises(IntegrityError):
        sync_session.flush()


def test_task_defaults(sync_session: Session) -> None:
    post, account = _make_world(sync_session)
    task = PublicationTask(post=post, account=account)
    sync_session.add(task)
    sync_session.flush()
    assert task.status == TaskStatus.PENDING
    assert task.attempt_count == 0
    assert task.created_at is not None
    assert task.scheduled_at is None


def test_task_unique_post_account(sync_session: Session) -> None:
    post, account = _make_world(sync_session)
    sync_session.add(PublicationTask(post=post, account=account))
    sync_session.flush()
    sync_session.add(PublicationTask(post=post, account=account))
    with pytest.raises(IntegrityError):
        sync_session.flush()


def test_invalid_task_status_rejected(sync_session: Session) -> None:
    post, account = _make_world(sync_session)
    sync_session.add(PublicationTask(post=post, account=account, status="weird"))
    with pytest.raises(IntegrityError):
        sync_session.flush()


def test_attempt_unique_number(sync_session: Session) -> None:
    post, account = _make_world(sync_session)
    task = PublicationTask(post=post, account=account)
    sync_session.add(task)
    sync_session.flush()
    sync_session.add(
        PublicationAttempt(task=task, attempt_number=1, started_at=datetime.now(UTC))
    )
    sync_session.flush()
    sync_session.add(
        PublicationAttempt(task=task, attempt_number=1, started_at=datetime.now(UTC))
    )
    with pytest.raises(IntegrityError):
        sync_session.flush()


def test_schedule_defaults(sync_session: Session) -> None:
    telegram = Platform(key="telegram", display_name="Telegram")
    account = Account(
        platform=telegram, key="a", display_name="A", credentials_ref="TELEGRAM_BOT_TOKEN"
    )
    schedule = PublishingSchedule(account=account, post_type="dialogue", publish_time=time(9, 0))
    sync_session.add(schedule)
    sync_session.flush()
    assert schedule.timezone == "UTC"
    assert schedule.days_of_week == [1, 2, 3, 4, 5, 6, 7]
    assert schedule.active is True


async def test_sqlite_foreign_keys_enforced() -> None:
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    factory = make_session_factory(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add(PublicationTask(post_id="missing-post", account_id="missing-account"))
        with pytest.raises(IntegrityError):
            await session.flush()
    await engine.dispose()
