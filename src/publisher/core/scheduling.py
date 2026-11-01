"""Schedule assignment (specification §9, roadmap step 4).

Platform-independent core: matches `pending` tasks without `scheduled_at`
to active account schedules, computes the next eligible slot in the
schedule's IANA time zone from ISO weekdays (1=Monday … 7=Sunday), converts
the instant to UTC, and persists it together with the `scheduled` status
(spec §9.1). Tasks that already carry a `scheduled_at` are never moved by
routine runs, and tasks without any matching active schedule stay `pending`.

DST policy (PLAN step-4 proposal — requires owner sign-off at merge):
- Ambiguous local times (repeated during a fall-back transition) resolve to
  the EARLIER offset.
- Nonexistent local times (skipped by a spring-forward transition) resolve
  to the NEXT VALID INSTANT: the end of the gap, i.e. the first instant at
  which the local clock shows a valid time again and the requested wall
  time has passed.

Slot exclusivity (spec §9.2): at most one task may occupy an account slot.
The invariant is enforced twice — by a database-level unique index on
`(account_id, scheduled_at)` (SQL NULL semantics keep unscheduled rows
unaffected) and by skipping occupied slots inside the single assignment
transaction, including slots claimed earlier in the same run.

The APScheduler-based `SchedulerService` runs the assignment pass
periodically; due-task publishing joins this tick in roadmap step 5.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from ..db.models import PublicationTask, PublishingSchedule, TaskStatus
from ..db.session import session_scope

logger = logging.getLogger(__name__)

#: ISO weekdays a schedule may name (1=Monday … 7=Sunday, spec §9.1).
ISO_WEEKDAYS = frozenset(range(1, 8))

#: How many days ahead a schedule is scanned for a free slot.
SLOT_SEARCH_HORIZON_DAYS = 14


# --------------------------------------------------------------------------
# Local-time resolution (DST policy)
# --------------------------------------------------------------------------


def resolve_local_time(naive: datetime, tz: ZoneInfo) -> datetime:
    """Resolve a naive local wall time in `tz` to a UTC instant.

    Implements the PLAN step-4 DST proposal (see module docstring):
    ambiguous wall times take the earlier offset (fold=0); nonexistent wall
    times resolve to the end of the gap.
    """
    aware_early = naive.replace(tzinfo=tz, fold=0)
    roundtrip = (
        aware_early.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    )
    if roundtrip != naive:
        # Nonexistent wall time: the gap end lies between the two fold
        # interpretations (fold=1 instant is before the transition, fold=0
        # instant after it).
        return _gap_end_instant(
            naive.replace(tzinfo=tz, fold=1).astimezone(UTC),
            aware_early.astimezone(UTC),
            tz,
        )
    # Ambiguous wall time: offsets differ between folds; fold=0 already
    # selects the earlier offset, which is the documented policy.
    return aware_early.astimezone(UTC)


def _gap_end_instant(before: datetime, after: datetime, tz: ZoneInfo) -> datetime:
    """First instant in `(before, after]` with an offset different from
    `before`'s — the transition that ends a skipped-time gap.

    Binary search over UTC instants; transitions are at whole seconds, so
    the result is exact for real time zones.
    """
    offset_before = before.astimezone(tz).utcoffset()
    lo, hi = before, after
    if hi <= lo:  # degenerate bounds: fall back to the later interpretation
        return after
    while hi - lo > timedelta(seconds=1):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == offset_before:
            lo = mid
        else:
            hi = mid
    return hi


# --------------------------------------------------------------------------
# Slot computation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleWindow:
    """Validated schedule fields needed for slot computation.

    Built from `PublishingSchedule` rows so the core never trusts raw rows:
    an unusable row (unknown zone, no valid ISO weekday) never reaches the
    slot math.
    """

    schedule_id: str
    publish_time: time
    tz: ZoneInfo
    days: frozenset[int]

    @classmethod
    def from_row(cls, schedule: PublishingSchedule) -> ScheduleWindow | None:
        """Parse a schedule row; `None` when the row is not usable."""
        try:
            tz = ZoneInfo(schedule.timezone)
        except (ZoneInfoNotFoundError, ValueError, AttributeError):
            return None
        days = frozenset(
            day for day in (schedule.days_of_week or []) if day in ISO_WEEKDAYS
        )
        if not days or not isinstance(schedule.publish_time, time):
            return None
        return cls(schedule.id, schedule.publish_time, tz, days)

    def iter_slots(self, *, now: datetime) -> Iterator[datetime]:
        """Yield future slot instants (UTC), earliest first, within the
        horizon. Slots whose wall time was skipped by DST resolve per the
        documented policy, so every yielded value is a real instant."""
        local_now = now.astimezone(self.tz)
        for day_offset in range(SLOT_SEARCH_HORIZON_DAYS + 1):
            day = (local_now + timedelta(days=day_offset)).date()
            if day.isoweekday() not in self.days:
                continue
            instant = resolve_local_time(
                datetime.combine(day, self.publish_time), self.tz
            )
            if instant >= now:
                yield instant


# --------------------------------------------------------------------------
# Assignment service
# --------------------------------------------------------------------------


@dataclass
class SchedulingOutcome:
    """Result of one assignment pass, keyed by task id only (spec §16:
    no post content in logs)."""

    assigned: list[str] = field(default_factory=list)
    left_pending: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


async def assign_pending_tasks(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> SchedulingOutcome:
    """Assign schedule slots to unscheduled pending tasks in one transaction
    (spec §9.1/§9.2)."""
    async with session_scope(factory) as session:
        return await _assign_within_session(session, now=now or datetime.now(UTC))


async def _assign_within_session(
    session: AsyncSession, *, now: datetime
) -> SchedulingOutcome:
    now = _as_utc(now)
    outcome = SchedulingOutcome()

    tasks = (
        (
            await session.execute(
                select(PublicationTask)
                .options(joinedload(PublicationTask.post))
                .where(
                    PublicationTask.status == TaskStatus.PENDING.value,
                    PublicationTask.scheduled_at.is_(None),
                )
                .order_by(PublicationTask.created_at, PublicationTask.id)
            )
        )
        .scalars()
        .unique()
        .all()
    )
    if not tasks:
        return outcome

    schedules = (
        (
            await session.execute(
                select(PublishingSchedule).where(PublishingSchedule.active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    by_key: dict[tuple[str, str], list[ScheduleWindow]] = {}
    invalid_by_key: dict[tuple[str, str], list[str]] = {}
    for schedule in schedules:
        window = ScheduleWindow.from_row(schedule)
        key = (schedule.account_id, schedule.post_type)
        if window is None:
            # Unusable row (unknown zone, no valid ISO weekday): never reach
            # the slot math, but report it instead of silently treating it
            # as "no schedule".
            invalid_by_key.setdefault(key, []).append(schedule.id)
        else:
            by_key.setdefault(key, []).append(window)

    occupied: dict[str, set[datetime]] = {}
    rows = await session.execute(
        select(PublicationTask.account_id, PublicationTask.scheduled_at).where(
            PublicationTask.scheduled_at.is_not(None),
            PublicationTask.scheduled_at >= now,
        )
    )
    for account_id, scheduled_at in rows.all():
        occupied.setdefault(account_id, set()).add(_as_utc(scheduled_at))

    for task in tasks:
        key = (task.account_id, task.post.post_type)
        windows_for_task = by_key.get(key, [])
        if not windows_for_task:
            if key in invalid_by_key:
                outcome.skipped[task.id] = (
                    "matching schedules have invalid configuration "
                    f"(schedule ids: {', '.join(sorted(invalid_by_key[key]))})"
                )
            else:
                outcome.left_pending.append(task.id)
            continue
        instant = _next_free_slot(
            windows_for_task, occupied.setdefault(task.account_id, set()), now=now
        )
        if instant is None:
            outcome.skipped[task.id] = (
                "no free schedule slot within the search horizon"
            )
            continue
        task.scheduled_at = instant
        task.status = TaskStatus.SCHEDULED.value
        occupied[task.account_id].add(instant)
        outcome.assigned.append(task.id)
        logger.info(
            "schedule assignment: task %s -> %s (account %s)",
            task.id,
            instant.isoformat(),
            task.account_id,
        )

    for task_id in outcome.left_pending:
        logger.info("schedule assignment: task %s has no active schedule; left pending", task_id)
    for task_id, reason in outcome.skipped.items():
        logger.warning("schedule assignment: task %s skipped: %s", task_id, reason)
    return outcome


def _next_free_slot(
    windows: list[ScheduleWindow],
    taken: set[datetime],
    *,
    now: datetime,
) -> datetime | None:
    """Earliest future slot across the task's schedules that no task of the
    account occupies yet (spec §9.2 slot exclusivity)."""
    candidates = sorted(
        {instant for window in windows for instant in window.iter_slots(now=now)}
    )
    for instant in candidates:
        if instant not in taken:
            return instant
    return None


async def compute_next_free_slot(
    session: AsyncSession,
    *,
    account_id: str,
    post_type: str,
    now: datetime,
) -> datetime | None:
    """Next future slot for `(account, post_type)` not occupied by another
    task of the account (spec §9.2).

    Used by the missed-slot downtime policy to relocate already-scheduled
    tasks whose slot passed during downtime. Occupied slots are tasks with
    `scheduled_at >= now`; the relocated task's own past slot no longer
    counts. Returns `None` when the account has no usable active schedule
    or no free slot within the horizon.
    """
    now = _as_utc(now)
    schedules = (
        (
            await session.execute(
                select(PublishingSchedule).where(
                    PublishingSchedule.account_id == account_id,
                    PublishingSchedule.post_type == post_type,
                    PublishingSchedule.active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    windows = [
        window
        for schedule in schedules
        if (window := ScheduleWindow.from_row(schedule)) is not None
    ]
    if not windows:
        return None
    rows = await session.execute(
        select(PublicationTask.scheduled_at).where(
            PublicationTask.account_id == account_id,
            PublicationTask.scheduled_at.is_not(None),
            PublicationTask.scheduled_at >= now,
        )
    )
    occupied = {_as_utc(value) for (value,) in rows.all()}
    return _next_free_slot(windows, occupied, now=now)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# --------------------------------------------------------------------------
# Periodic scheduler (APScheduler)
# --------------------------------------------------------------------------


class SchedulerService:
    """Periodic schedule-assignment pass backed by APScheduler.

    One interval job runs `tick()`; the due-task publishing pass joins this
    tick when the publishing engine lands (roadmap step 5). Construction is
    side-effect free; `start()` requires a running asyncio loop.
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: int = 15,
        enabled: bool = True,
    ) -> None:
        self._factory = factory
        self._interval_seconds = max(1, int(interval_seconds))
        self._enabled = enabled
        self._scheduler: AsyncIOScheduler | None = None
        self.last_outcome: SchedulingOutcome | None = None

    async def tick(self) -> SchedulingOutcome:
        """Run one assignment pass and remember its summary."""
        outcome = await assign_pending_tasks(self._factory)
        self.last_outcome = outcome
        logger.info(
            "scheduler tick: assigned=%d left_pending=%d skipped=%d",
            len(outcome.assigned),
            len(outcome.left_pending),
            len(outcome.skipped),
        )
        return outcome

    def start(self) -> None:
        """Start the interval job on the running event loop."""
        if not self._enabled or self._scheduler is not None:
            return
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self.tick,
            trigger="interval",
            seconds=self._interval_seconds,
            id="upp-schedule-assignment",
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info(
            "scheduler started (interval=%ds)", self._interval_seconds
        )

    async def shutdown(self) -> None:
        """Stop the interval job; completed ticks are never interrupted."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("scheduler stopped")
