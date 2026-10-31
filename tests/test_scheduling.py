"""Schedule assignment tests (roadmap step 4, specification §9/§17).

Covers the §17 scheduling items: account/post-type matching, time-zone
conversion with UTC persistence, DST ambiguous/nonexistent instants,
multiple schedule slots, ISO weekdays, no-schedule leaves pending, already
scheduled tasks are not reassigned, missed-slot skipping, and slot
exclusivity (application level plus the database unique index). All runs
execute against in-memory SQLite — no real scheduler timing, no external
services.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from publisher.core.scheduling import (
    SchedulerService,
    SchedulingOutcome,
    assign_pending_tasks,
    resolve_local_time,
)
from publisher.db.models import (
    Account,
    Platform,
    Post,
    PublicationTask,
    PublishingSchedule,
    TaskStatus,
)
from publisher.db.session import session_scope

ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]


def _utc(value: datetime) -> datetime:
    """Normalize a value read back from SQLite (naive == UTC by convention)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------
# Seed helpers
# --------------------------------------------------------------------------


async def _seed_account(
    factory: async_sessionmaker[AsyncSession],
    *,
    key: str = "english_channel",
) -> str:
    async with session_scope(factory) as session:
        platform = (
            await session.scalars(select(Platform).where(Platform.key == "telegram"))
        ).first()
        if platform is None:
            platform = Platform(key="telegram", display_name="Telegram")
        account = Account(
            platform=platform,
            key=key,
            display_name=key.title(),
            credentials_ref="TELEGRAM_BOT_TOKEN",
            configuration_json={"chat_id": "100"},
        )
        session.add(account)
        await session.flush()
        return account.id


async def _seed_schedule(
    factory: async_sessionmaker[AsyncSession],
    account_id: str,
    *,
    post_type: str = "dialogue",
    publish_time: time = time(9, 0),
    timezone_name: str = "Asia/Yekaterinburg",
    days: list[int] | None = None,
    active: bool = True,
) -> str:
    async with session_scope(factory) as session:
        schedule = PublishingSchedule(
            account_id=account_id,
            post_type=post_type,
            publish_time=publish_time,
            timezone=timezone_name,
            days_of_week=ALL_DAYS if days is None else days,
            active=active,
        )
        session.add(schedule)
        await session.flush()
        return schedule.id


async def _seed_task(
    factory: async_sessionmaker[AsyncSession],
    account_id: str,
    *,
    post_type: str = "dialogue",
) -> str:
    async with session_scope(factory) as session:
        post = Post(
            source="test",
            external_id=f"ext-{uuid.uuid4()}",
            content_json={"type": "text", "body": "demo body", "format": "plain_text", "media": []},
            post_type=post_type,
        )
        task = PublicationTask(
            post=post, account_id=account_id, status=TaskStatus.PENDING.value
        )
        session.add(task)
        await session.flush()
        return task.id


async def _seed_scheduled_task(
    factory: async_sessionmaker[AsyncSession],
    account_id: str,
    *,
    scheduled_at: datetime,
    status: str = TaskStatus.SCHEDULED.value,
) -> str:
    async with session_scope(factory) as session:
        post = Post(
            source="test",
            external_id=f"ext-{uuid.uuid4()}",
            content_json={"type": "text", "body": "demo body", "format": "plain_text", "media": []},
            post_type="dialogue",
        )
        task = PublicationTask(
            post=post,
            account_id=account_id,
            status=status,
            scheduled_at=scheduled_at,
        )
        session.add(task)
        await session.flush()
        return task.id


async def _get_task(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> PublicationTask:
    async with factory() as session:
        return await session.get(PublicationTask, task_id)


# --------------------------------------------------------------------------
# DST resolution (pure function)
# --------------------------------------------------------------------------


def test_ambiguous_local_time_takes_earlier_offset() -> None:
    london = ZoneInfo("Europe/London")
    # 2026-10-25 01:30 local happens twice (BST +1 then GMT +0).
    assert resolve_local_time(datetime(2026, 10, 25, 1, 30), london) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


def test_ambiguous_local_time_new_york_earlier_offset() -> None:
    new_york = ZoneInfo("America/New_York")
    # 2026-11-01 01:30 local happens twice (EDT -4 then EST -5).
    assert resolve_local_time(datetime(2026, 11, 1, 1, 30), new_york) == datetime(
        2026, 11, 1, 5, 30, tzinfo=UTC
    )


def test_nonexistent_local_time_resolves_to_gap_end() -> None:
    london = ZoneInfo("Europe/London")
    # 2026-03-29 01:30 local does not exist (01:00 GMT -> 02:00 BST); the
    # next valid instant is the end of the gap, 01:00 UTC.
    assert resolve_local_time(datetime(2026, 3, 29, 1, 30), london) == datetime(
        2026, 3, 29, 1, 0, tzinfo=UTC
    )


def test_nonexistent_local_time_new_york_gap_end() -> None:
    new_york = ZoneInfo("America/New_York")
    # 2026-03-08 02:30 local does not exist (02:00 EST -> 03:00 EDT).
    assert resolve_local_time(datetime(2026, 3, 8, 2, 30), new_york) == datetime(
        2026, 3, 8, 7, 0, tzinfo=UTC
    )


def test_unambiguous_local_time_resolves_normally() -> None:
    yekaterinburg = ZoneInfo("Asia/Yekaterinburg")  # UTC+5, no DST
    assert resolve_local_time(datetime(2026, 11, 2, 9, 0), yekaterinburg) == datetime(
        2026, 11, 2, 4, 0, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# §17 scheduling items through the assignment service
# --------------------------------------------------------------------------


async def test_matches_account_and_post_type(session_factory) -> None:
    account_a = await _seed_account(session_factory, key="channel_a")
    account_b = await _seed_account(session_factory, key="channel_b")
    await _seed_schedule(session_factory, account_a, post_type="dialogue")
    target = await _seed_task(session_factory, account_a, post_type="dialogue")
    other_type = await _seed_task(session_factory, account_a, post_type="grammar")
    other_account = await _seed_task(session_factory, account_b, post_type="dialogue")

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert outcome.assigned == [target]
    assert set(outcome.left_pending) == {other_type, other_account}
    assert outcome.skipped == {}
    for task_id in (other_type, other_account):
        task = await _get_task(session_factory, task_id)
        assert task.status == TaskStatus.PENDING.value
        assert task.scheduled_at is None


async def test_time_zone_conversion_persists_utc(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, publish_time=time(9, 0))
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert outcome.assigned == [task_id]
    task = await _get_task(session_factory, task_id)
    # 09:00 Asia/Yekaterinburg (UTC+5) == 04:00 UTC, stored per the UTC
    # persistence convention (naive read-back == UTC).
    assert _utc(task.scheduled_at) == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    assert task.status == TaskStatus.SCHEDULED.value


@pytest.mark.parametrize(
    ("timezone_name", "now", "expected"),
    [
        ("Europe/London", datetime(2026, 10, 24, 12, 0), datetime(2026, 10, 25, 0, 30)),
        ("America/New_York", datetime(2026, 10, 31, 12, 0), datetime(2026, 11, 1, 5, 30)),
    ],
)
async def test_dst_ambiguous_slot_takes_earlier_offset(
    session_factory, timezone_name, now, expected
) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(
        session_factory, account_id, publish_time=time(1, 30), timezone_name=timezone_name
    )
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(session_factory, now=now.replace(tzinfo=UTC))

    assert outcome.assigned == [task_id]
    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == expected.replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("timezone_name", "publish_time", "now", "expected"),
    [
        ("Europe/London", time(1, 30), datetime(2026, 3, 28, 12, 0), datetime(2026, 3, 29, 1, 0)),
        ("America/New_York", time(2, 30), datetime(2026, 3, 7, 12, 0), datetime(2026, 3, 8, 7, 0)),
    ],
)
async def test_dst_nonexistent_slot_resolves_to_next_valid_instant(
    session_factory, timezone_name, publish_time, now, expected
) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(
        session_factory, account_id, publish_time=publish_time, timezone_name=timezone_name
    )
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(session_factory, now=now.replace(tzinfo=UTC))

    assert outcome.assigned == [task_id]
    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == expected.replace(tzinfo=UTC)


async def test_iso_weekday_selection(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(
        session_factory,
        account_id,
        publish_time=time(9, 0),
        timezone_name="UTC",
        days=[7],  # ISO Sunday
    )
    task_id = await _seed_task(session_factory, account_id)

    # 2026-10-30 is a Friday; the next Sunday is 2026-11-01.
    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
    )

    assert outcome.assigned == [task_id]
    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


async def test_iso_weekday_monday_is_one(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(
        session_factory, account_id, publish_time=time(9, 0), timezone_name="UTC", days=[1]
    )
    task_id = await _seed_task(session_factory, account_id)

    # 2026-11-01 is a Sunday; the next Monday is 2026-11-02.
    await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    )

    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


async def test_multiple_schedule_slots_earliest_wins(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, publish_time=time(9, 0), timezone_name="UTC")
    await _seed_schedule(
        session_factory, account_id, publish_time=time(18, 30), timezone_name="UTC"
    )
    task_id = await _seed_task(session_factory, account_id)

    # After the morning slot, the same day's evening slot is the earliest.
    await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    )

    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == datetime(2026, 11, 2, 18, 30, tzinfo=UTC)


async def test_no_schedule_leaves_task_pending(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    )

    assert outcome.assigned == []
    assert outcome.left_pending == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PENDING.value
    assert task.scheduled_at is None


async def test_inactive_schedule_ignored(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, active=False)
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
    )

    assert outcome.left_pending == [task_id]


async def test_existing_scheduled_tasks_not_reassigned(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    pinned = datetime(2026, 11, 9, 4, 0)
    scheduled_id = await _seed_scheduled_task(
        session_factory, account_id, scheduled_at=pinned
    )
    await _seed_task(session_factory, account_id)  # fresh pending task

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    scheduled = await _get_task(session_factory, scheduled_id)
    assert _utc(scheduled.scheduled_at) == pinned.replace(tzinfo=UTC)
    assert scheduled.status == TaskStatus.SCHEDULED.value
    assert scheduled_id not in outcome.assigned


async def test_pending_task_with_assigned_time_untouched(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    # Odd state that a routine run must never touch: pending but carrying a
    # scheduled_at (only the owner of that state may change it).
    odd_id = await _seed_scheduled_task(
        session_factory,
        account_id,
        scheduled_at=datetime(2026, 11, 9, 4, 0),
        status=TaskStatus.PENDING.value,
    )

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    odd = await _get_task(session_factory, odd_id)
    assert _utc(odd.scheduled_at) == datetime(2026, 11, 9, 4, 0, tzinfo=UTC)
    assert odd.status == TaskStatus.PENDING.value
    assert odd_id not in outcome.assigned + outcome.left_pending + list(outcome.skipped)


async def test_missed_slots_are_skipped(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, publish_time=time(9, 0))
    task_id = await _seed_task(session_factory, account_id)

    # 12:00 UTC == 17:00 local: today's 09:00 slot is already gone.
    await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    )

    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == datetime(2026, 11, 3, 4, 0, tzinfo=UTC)


async def test_no_two_tasks_share_a_slot_within_one_run(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, publish_time=time(9, 0))
    first = await _seed_task(session_factory, account_id)
    second = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert set(outcome.assigned) == {first, second}
    first_task = await _get_task(session_factory, first)
    second_task = await _get_task(session_factory, second)
    assert _utc(first_task.scheduled_at) == datetime(2026, 11, 2, 4, 0, tzinfo=UTC)
    assert _utc(second_task.scheduled_at) == datetime(2026, 11, 3, 4, 0, tzinfo=UTC)


async def test_occupied_slot_skipped_against_existing_task(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, publish_time=time(9, 0))
    await _seed_scheduled_task(
        session_factory, account_id, scheduled_at=datetime(2026, 11, 2, 4, 0)
    )
    pending_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert outcome.assigned == [pending_id]
    task = await _get_task(session_factory, pending_id)
    # The next day's slot; the occupied one is never reused.
    assert _utc(task.scheduled_at) == datetime(2026, 11, 3, 4, 0, tzinfo=UTC)


async def test_slot_uniqueness_enforced_by_database(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_scheduled_task(
        session_factory, account_id, scheduled_at=datetime(2026, 11, 2, 4, 0)
    )

    with pytest.raises(IntegrityError):
        async with session_scope(session_factory) as session:
            post = Post(
                source="test",
                external_id=f"ext-{uuid.uuid4()}",
                content_json={"type": "text", "body": "x", "format": "plain_text", "media": []},
                post_type="dialogue",
            )
            session.add(
                PublicationTask(
                    post=post,
                    account_id=account_id,
                    status=TaskStatus.SCHEDULED.value,
                    scheduled_at=datetime(2026, 11, 2, 4, 0),
                )
            )


async def test_invalid_timezone_schedule_skipped(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, timezone_name="Mars/Olympus")
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert outcome.assigned == []
    assert task_id in outcome.skipped
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PENDING.value


@pytest.mark.parametrize("days", [[], [0, 99], [8]])
async def test_invalid_weekday_sets_skipped(session_factory, days) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id, timezone_name="UTC", days=days)
    task_id = await _seed_task(session_factory, account_id)

    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )

    assert outcome.assigned == []
    assert task_id in outcome.skipped


async def test_empty_database_is_a_noop(session_factory) -> None:
    outcome = await assign_pending_tasks(
        session_factory, now=datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    )
    assert outcome == SchedulingOutcome()


# --------------------------------------------------------------------------
# APScheduler service
# --------------------------------------------------------------------------


async def test_scheduler_service_tick_assigns(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    task_id = await _seed_task(session_factory, account_id)

    service = SchedulerService(session_factory, interval_seconds=15)
    outcome = await service.tick()

    assert outcome.assigned == [task_id]
    assert service.last_outcome is outcome


async def test_scheduler_service_start_and_shutdown(session_factory) -> None:
    service = SchedulerService(session_factory, interval_seconds=3600)
    assert service.last_outcome is None

    service.start()
    assert service._scheduler is not None
    assert len(service._scheduler.get_jobs()) == 1
    job = service._scheduler.get_jobs()[0]
    assert job.id == "upp-schedule-assignment"

    service.start()  # idempotent
    assert len(service._scheduler.get_jobs()) == 1

    await service.shutdown()
    assert service._scheduler is None


async def test_scheduler_service_disabled_never_starts(session_factory) -> None:
    service = SchedulerService(session_factory, interval_seconds=15, enabled=False)
    service.start()
    assert service._scheduler is None
