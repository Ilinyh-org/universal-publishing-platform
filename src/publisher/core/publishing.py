"""Publishing engine (specification §9.2, §9.3, §10 — roadmap step 5).

Platform-independent core: one periodic pass

1. recovers stale `publishing` claims (crash recovery, §9.3),
2. applies the missed-slot downtime policy (§9.2): when the gap since the
   previous engine tick exceeds the stale-claim timeout, due `scheduled`
   tasks are NOT published as a backlog — their missed slot is skipped and
   the task moves to its next future schedule slot, with the rescheduling
   recorded in the logs,
3. claims due tasks atomically (conditional `UPDATE ... WHERE status =` so
   no two workers can ever hold the same task), creates the attempt record
   in the same transaction, calls the resolved adapter OUTSIDE any database
   transaction, and records success/retry/failure in a final transaction,
4. records the heartbeat so the next tick can distinguish routine
   operation from downtime.

Retry policy (§10): exponential backoff `base * 2^(attempt-1)` capped at
`RETRY_MAX_DELAY_SECONDS`, with jitter in the upper half of the computed
delay; platform-provided retry delays (`retry_after`) are honored exactly
and never jittered shorter. After `MAX_ATTEMPTS` total attempts (default 5)
a task is `failed`. Errors are classified by adapters; unclassified
exceptions are retried conservatively and bounded by the same attempt cap.
Exactly-once delivery is not promised: a crash between platform acceptance
and local success can duplicate an external post, and the attempt history
records that ambiguity.

One task failing never blocks another: each due task is claimed, published,
and finalized independently.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload

from ..adapters.registry import UnknownPlatformError
from ..core.adapters import AdapterError, ErrorClassification, PlatformAdapter
from ..core.content import UniversalContent
from ..core.secrets import SecretProvider, build_account_config
from ..db.models import (
    Account,
    EngineState,
    PublicationAttempt,
    PublicationTask,
    TaskStatus,
)
from ..db.session import session_scope
from .config import Settings
from .scheduling import compute_next_free_slot

logger = logging.getLogger(__name__)

#: Singleton heartbeat row id in `engine_state`.
HEARTBEAT_ID = 1

#: Jitter band for computed backoff delays: the delay is drawn from the
#: upper half of the exponential value, so retries never fire immediately.
JITTER_FRACTION = 0.5

#: Stable error code recorded on stale-claim recovery (spec §9.3).
STALE_CLAIM_CODE = "stale_claim"

#: Upper bound waited for an in-flight tick during shutdown.
SHUTDOWN_DRAIN_SECONDS = 30.0


# --------------------------------------------------------------------------
# Configuration and outcomes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineConfig:
    """Engine tuning values; defaults mirror `.env.example` (spec §14)."""

    max_attempts: int = 5
    retry_base_delay_seconds: int = 30
    retry_max_delay_seconds: int = 3600
    stale_publishing_timeout_seconds: int = 900

    @classmethod
    def from_settings(cls, settings: Settings) -> EngineConfig:
        return cls(
            max_attempts=settings.max_attempts,
            retry_base_delay_seconds=settings.retry_base_delay_seconds,
            retry_max_delay_seconds=settings.retry_max_delay_seconds,
            stale_publishing_timeout_seconds=settings.stale_publishing_timeout_seconds,
        )


@dataclass
class EngineOutcome:
    """One engine pass, keyed by task id only (spec §16: no content in logs)."""

    published: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stale_recovered: list[str] = field(default_factory=list)
    rescheduled: list[str] = field(default_factory=list)
    #: Due `scheduled` tasks kept as-is because no future slot exists for
    #: them (no usable active schedule); they stay due and publish normally.
    missed_kept: list[str] = field(default_factory=list)
    #: Tasks not processed this pass, with the reason.
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def any_activity(self) -> bool:
        return bool(
            self.published
            or self.retried
            or self.failed
            or self.stale_recovered
            or self.rescheduled
            or self.missed_kept
            or self.skipped
        )


@runtime_checkable
class RandomRange(Protocol):
    """`random.uniform`-shaped callable, injectable for deterministic tests."""

    def __call__(self, a: float, b: float) -> float: ...


def compute_backoff_delay(
    attempt_number: int,
    *,
    base_seconds: int,
    max_seconds: int,
    rng: RandomRange = random.uniform,
) -> float:
    """Exponential backoff with jitter for the given (1-based) attempt.

    `base * 2^(attempt-1)` capped at `max_seconds`; jitter draws from the
    upper half of the value so a retry never fires at ~zero delay (§10).
    """
    attempt = max(1, attempt_number)
    exponential = float(base_seconds) * (2 ** (attempt - 1))
    capped = min(exponential, float(max_seconds))
    return max(0.0, rng(capped * JITTER_FRACTION, capped))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# --------------------------------------------------------------------------
# Atomic claim
# --------------------------------------------------------------------------


async def claim_task(
    factory: async_sessionmaker[AsyncSession],
    task_id: str,
    *,
    expected_status: str,
    now: datetime,
) -> int | None:
    """Atomically claim a task into `publishing` (spec §9.3).

    A single conditional `UPDATE` flips the status only if it still equals
    `expected_status`; a concurrent tick's identical claim matches zero rows.
    The attempt record is created in the same transaction. Returns the new
    1-based attempt number, or `None` when the task was claimed/finalized by
    someone else in the meantime.
    """
    now = _as_utc(now)
    async with session_scope(factory) as session:
        result = await session.execute(
            update(PublicationTask)
            .where(
                PublicationTask.id == task_id,
                PublicationTask.status == expected_status,
            )
            .values(
                status=TaskStatus.PUBLISHING.value,
                claimed_at=now,
                updated_at=now,
                attempt_count=PublicationTask.attempt_count + 1,
            )
        )
        if result.rowcount != 1:
            return None
        task = await session.get(PublicationTask, task_id)
        if task is None:  # pragma: no cover - row just updated in this tx
            return None
        session.add(
            PublicationAttempt(
                task_id=task_id, attempt_number=task.attempt_count, started_at=now
            )
        )
        return task.attempt_count


# --------------------------------------------------------------------------
# Stale-claim recovery (spec §9.3)
# --------------------------------------------------------------------------


async def recover_stale_claims(
    factory: async_sessionmaker[AsyncSession],
    config: EngineConfig,
    *,
    now: datetime,
    rng: RandomRange = random.uniform,
) -> list[str]:
    """Return tasks stuck in `publishing` past the claim timeout to the queue.

    The unfinished attempt is closed with `stale_claim` and a message that
    records the delivery ambiguity: the platform may have accepted the
    request, so a duplicate external post is possible on retry (spec §9.3,
    §10). Tasks at `MAX_ATTEMPTS` are failed; others go to `retry_wait`.
    """
    now = _as_utc(now)
    cutoff = now - timedelta(seconds=config.stale_publishing_timeout_seconds)
    async with session_scope(factory) as session:
        stuck = (
            (
                await session.execute(
                    select(PublicationTask.id, PublicationTask.attempt_count)
                    .where(PublicationTask.status == TaskStatus.PUBLISHING.value)
                    .where(
                        or_(
                            PublicationTask.claimed_at.is_(None),
                            PublicationTask.claimed_at <= cutoff,
                        )
                    )
                    .order_by(PublicationTask.id)
                )
            )
            .all()
        )

    recovered: list[str] = []
    for task_id, attempt_count in stuck:
        exhausted = attempt_count >= config.max_attempts
        message = (
            "claim expired after the stale-claim timeout; the platform may have "
            "accepted the request, so a duplicate external post is possible"
        )
        async with session_scope(factory) as session:
            values: dict[str, object] = {
                "status": (
                    TaskStatus.FAILED.value
                    if exhausted
                    else TaskStatus.RETRY_WAIT.value
                ),
                "claimed_at": None,
                "last_error_code": STALE_CLAIM_CODE,
                "last_error_message": message,
                "updated_at": now,
            }
            if not exhausted:
                delay = compute_backoff_delay(
                    attempt_count,
                    base_seconds=config.retry_base_delay_seconds,
                    max_seconds=config.retry_max_delay_seconds,
                    rng=rng,
                )
                values["next_attempt_at"] = now + timedelta(seconds=delay)
            result = await session.execute(
                update(PublicationTask)
                .where(
                    PublicationTask.id == task_id,
                    PublicationTask.status == TaskStatus.PUBLISHING.value,
                )
                .values(**values)
            )
            if result.rowcount != 1:  # finalized by another worker meanwhile
                continue
            attempt = await _get_attempt(session, task_id, attempt_count)
            if attempt is not None and attempt.success is None:
                attempt.success = False
                attempt.finished_at = now
                attempt.error_code = STALE_CLAIM_CODE
                attempt.error_message = message
        recovered.append(task_id)
        if exhausted:
            logger.error(
                "engine: stale claim recovered for task %s (attempt %d); max "
                "attempts exhausted, task failed — delivery outcome unknown "
                "(possible duplicate, spec §9.3)",
                task_id,
                attempt_count,
            )
        else:
            logger.warning(
                "engine: stale claim recovered for task %s (attempt %d); "
                "returned to the retry queue — delivery outcome unknown "
                "(possible duplicate, spec §9.3)",
                task_id,
                attempt_count,
            )
    return recovered


# --------------------------------------------------------------------------
# Missed-slot downtime policy (spec §9.2)
# --------------------------------------------------------------------------


async def reschedule_missed_slots(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    downtime_threshold_seconds: int,
) -> tuple[list[str], list[str]]:
    """Skip slots missed during downtime; move due work to future slots.

    A gap since the previous heartbeat larger than
    `downtime_threshold_seconds` (or a first tick with no heartbeat at all)
    counts as downtime: due `scheduled` tasks are moved to their next future
    schedule slot instead of being published as a burst, and every move is
    logged (§9.2). Returns `(rescheduled_task_ids, kept_due_task_ids)`.
    """
    now = _as_utc(now)
    last_tick: datetime | None = None
    for _ in range(2):  # insert race on the singleton row: retry as a read
        try:
            async with session_scope(factory) as session:
                state = await session.get(EngineState, HEARTBEAT_ID)
                if state is None:
                    session.add(
                        EngineState(id=HEARTBEAT_ID, last_tick_at=None, updated_at=now)
                    )
                last_tick = (
                    _as_utc(state.last_tick_at)
                    if state is not None and state.last_tick_at is not None
                    else None
                )
            break
        except IntegrityError:
            continue

    if (
        last_tick is not None
        and (now - last_tick).total_seconds() <= downtime_threshold_seconds
    ):
        return [], []  # routine operation: due tasks publish normally

    async with session_scope(factory) as session:
        due_ids = (
            (
                await session.execute(
                    select(PublicationTask.id)
                    .where(
                        PublicationTask.status == TaskStatus.SCHEDULED.value,
                        PublicationTask.scheduled_at.is_not(None),
                        PublicationTask.scheduled_at <= now,
                    )
                    .order_by(PublicationTask.scheduled_at, PublicationTask.id)
                )
            )
            .scalars()
            .all()
        )

    rescheduled: list[str] = []
    kept: list[str] = []
    for task_id in due_ids:
        try:
            moved = await _reschedule_one(factory, task_id, now=now)
        except IntegrityError:
            # Another worker moved this task to the same slot first; the
            # unique account-slot index (spec §9.2) rejected the duplicate.
            logger.info(
                "engine: task %s was rescheduled by another worker; slot move skipped",
                task_id,
            )
            continue
        if moved is None:
            kept.append(task_id)
            logger.warning(
                "engine: task %s missed its slot during downtime but has no "
                "usable active schedule for a future slot; leaving it due "
                "(spec §9.2)",
                task_id,
            )
            continue
        missed_slot, slot = moved
        rescheduled.append(task_id)
        logger.info(
            "engine: missed slot %s skipped for task %s during downtime; "
            "rescheduled to %s (spec §9.2)",
            missed_slot.isoformat(),
            task_id,
            slot.isoformat(),
        )
    if last_tick is None and not rescheduled and not kept:
        logger.info("engine: first tick recorded; downtime policy armed")
    return rescheduled, kept


async def _reschedule_one(
    factory: async_sessionmaker[AsyncSession],
    task_id: str,
    *,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Move one due scheduled task to its next future slot.

    Returns `(missed_slot, new_slot)`, or `None` when no future slot exists.
    """
    async with session_scope(factory) as session:
        task = await session.get(
            PublicationTask,
            task_id,
            options=[joinedload(PublicationTask.post)],
        )
        if (
            task is None
            or task.status != TaskStatus.SCHEDULED.value
            or task.scheduled_at is None
            or _as_utc(task.scheduled_at) > now
        ):
            return None  # claimed/finalized by another worker meanwhile
        slot = await compute_next_free_slot(
            session,
            account_id=task.account_id,
            post_type=task.post.post_type,
            now=now,
        )
        if slot is None:
            return None
        missed_slot = _as_utc(task.scheduled_at)
        task.scheduled_at = slot  # status stays `scheduled`
        return missed_slot, slot


# --------------------------------------------------------------------------
# Due-task processing (spec §9.3)
# --------------------------------------------------------------------------


async def process_due_tasks(
    factory: async_sessionmaker[AsyncSession],
    registry: AdapterRegistryLike,
    secrets: SecretProvider,
    config: EngineConfig,
    *,
    now: datetime | None = None,
    rng: RandomRange = random.uniform,
) -> EngineOutcome:
    """Claim and publish every due task once, independently.

    Due means `scheduled` with `scheduled_at <= now` or `retry_wait` with
    `next_attempt_at <= now`. Tasks on unknown or disabled platforms are
    skipped (left due, never claimed) with a clear log line.
    """
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    outcome = EngineOutcome()
    for task in await _load_due_tasks(factory, now=now):
        await _process_one(
            factory, registry, secrets, config, task, outcome, now=now, rng=rng
        )
    return outcome


async def run_engine_tick(
    factory: async_sessionmaker[AsyncSession],
    registry: AdapterRegistryLike,
    secrets: SecretProvider,
    config: EngineConfig,
    *,
    now: datetime | None = None,
    rng: RandomRange = random.uniform,
) -> EngineOutcome:
    """One full engine pass: stale recovery, missed-slot policy, due tasks,
    heartbeat."""
    now = _as_utc(now) if now is not None else datetime.now(UTC)
    outcome = EngineOutcome()
    outcome.stale_recovered = await recover_stale_claims(
        factory, config, now=now, rng=rng
    )
    outcome.rescheduled, outcome.missed_kept = await reschedule_missed_slots(
        factory,
        now=now,
        downtime_threshold_seconds=config.stale_publishing_timeout_seconds,
    )
    for task in await _load_due_tasks(factory, now=now):
        await _process_one(
            factory, registry, secrets, config, task, outcome, now=now, rng=rng
        )
    await _update_heartbeat(factory, now=now)
    if outcome.any_activity:
        logger.info(
            "engine tick: published=%d retried=%d failed=%d stale_recovered=%d "
            "rescheduled=%d missed_kept=%d skipped=%d",
            len(outcome.published),
            len(outcome.retried),
            len(outcome.failed),
            len(outcome.stale_recovered),
            len(outcome.rescheduled),
            len(outcome.missed_kept),
            len(outcome.skipped),
        )
    return outcome


@runtime_checkable
class AdapterRegistryLike(Protocol):
    """The registry surface the engine depends on (spec §11.3)."""

    def resolve(self, platform_key: str) -> PlatformAdapter: ...


async def _load_due_tasks(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> list[PublicationTask]:
    async with session_scope(factory) as session:
        due_scheduled = and_(
            PublicationTask.status == TaskStatus.SCHEDULED.value,
            PublicationTask.scheduled_at <= now,
        )
        due_retry = and_(
            PublicationTask.status == TaskStatus.RETRY_WAIT.value,
            PublicationTask.next_attempt_at <= now,
        )
        stmt = (
            select(PublicationTask)
            .options(
                joinedload(PublicationTask.post),
                joinedload(PublicationTask.account).joinedload(Account.platform),
            )
            .where(or_(due_scheduled, due_retry))
            .order_by(
                func.coalesce(
                    PublicationTask.scheduled_at, PublicationTask.next_attempt_at
                ),
                PublicationTask.id,
            )
        )
        return list((await session.execute(stmt)).scalars().unique().all())


async def _process_one(
    factory: async_sessionmaker[AsyncSession],
    registry: AdapterRegistryLike,
    secrets: SecretProvider,
    config: EngineConfig,
    task_row: PublicationTask,
    outcome: EngineOutcome,
    *,
    now: datetime,
    rng: RandomRange,
) -> None:
    """Claim, publish, and finalize exactly one task; failures never leak."""
    account = task_row.account
    platform = account.platform if account is not None else None
    if account is None or platform is None or not platform.enabled:
        outcome.skipped[task_row.id] = "platform unknown or disabled"
        logger.warning(
            "engine: task %s skipped: platform unknown or disabled", task_row.id
        )
        return
    try:
        adapter = registry.resolve(platform.key)
    except UnknownPlatformError as exc:
        outcome.skipped[task_row.id] = str(exc)
        logger.warning("engine: task %s skipped: %s", task_row.id, exc)
        return

    attempt_number = await claim_task(
        factory, task_row.id, expected_status=task_row.status, now=now
    )
    if attempt_number is None:
        return  # another worker claimed or finalized it first
    logger.info(
        "engine: claimed task %s (attempt %d/%d)",
        task_row.id,
        attempt_number,
        config.max_attempts,
    )

    # Network work happens outside any database transaction (ARCHITECTURE §8).
    try:
        content = UniversalContent.model_validate(task_row.post.content_json)
        account_config = build_account_config(
            account_key=account.key,
            platform_key=platform.key,
            configuration=account.configuration_json or {},
            credentials_ref=account.credentials_ref,
            adapter=adapter,
            secrets=secrets,
        )
        result = await adapter.publish(content, account_config)
    except AdapterError as exc:
        await _finalize_failure(
            factory,
            task_row.id,
            attempt_number,
            error=exc,
            config=config,
            outcome=outcome,
            now=now,
            rng=rng,
        )
        return
    except ValidationError:
        error = AdapterError(
            "invalid_content",
            "stored content failed universal content validation",
            ErrorClassification.PERMANENT,
        )
        await _finalize_failure(
            factory,
            task_row.id,
            attempt_number,
            error=error,
            config=config,
            outcome=outcome,
            now=now,
            rng=rng,
        )
        return
    except Exception as exc:
        # Unclassified failure (bug or transport outside the adapter):
        # conservative retry, bounded by MAX_ATTEMPTS. Exception text may
        # embed request details — only the class name is stored/logged here.
        logger.exception("engine: unexpected error publishing task %s", task_row.id)
        error = AdapterError(
            "unexpected_error",
            f"unexpected failure: {type(exc).__name__}",
            ErrorClassification.TEMPORARY,
        )
        await _finalize_failure(
            factory,
            task_row.id,
            attempt_number,
            error=error,
            config=config,
            outcome=outcome,
            now=now,
            rng=rng,
        )
        return
    await _finalize_success(
        factory, task_row.id, attempt_number, result=result, outcome=outcome, now=now
    )


async def _finalize_success(
    factory: async_sessionmaker[AsyncSession],
    task_id: str,
    attempt_number: int,
    *,
    result: object,
    outcome: EngineOutcome,
    now: datetime,
) -> None:
    external_post_id = getattr(result, "external_post_id", None)
    metadata = dict(getattr(result, "metadata", {}) or {})
    async with session_scope(factory) as session:
        task = await session.get(PublicationTask, task_id)
        if task is None or task.status != TaskStatus.PUBLISHING.value:
            logger.warning(
                "engine: task %s is no longer publishing; success discarded", task_id
            )
            return
        task.status = TaskStatus.PUBLISHED.value
        task.published_at = now
        if external_post_id:
            task.external_post_id = str(external_post_id)
        task.next_attempt_at = None
        task.claimed_at = None
        task.last_error_code = None
        task.last_error_message = None
        task.updated_at = now
        attempt = await _get_attempt(session, task_id, attempt_number)
        if attempt is not None and attempt.success is None:
            attempt.success = True
            attempt.finished_at = now
            # Adapters guarantee sanitized metadata (spec §11.2).
            attempt.response_metadata_json = metadata
    outcome.published.append(task_id)
    logger.info(
        "engine: task %s published (attempt %d%s)",
        task_id,
        attempt_number,
        f", external_post_id={external_post_id}" if external_post_id else "",
    )


async def _finalize_failure(
    factory: async_sessionmaker[AsyncSession],
    task_id: str,
    attempt_number: int,
    *,
    error: AdapterError,
    config: EngineConfig,
    outcome: EngineOutcome,
    now: datetime,
    rng: RandomRange,
) -> None:
    exhausted = attempt_number >= config.max_attempts
    terminal = exhausted or error.classification == ErrorClassification.PERMANENT
    retry_delay: float | None = None
    if not terminal:
        if (
            error.classification == ErrorClassification.RATE_LIMITED
            and error.retry_after is not None
        ):
            # Platform-provided delay is honored exactly (spec §10).
            retry_delay = max(0.0, error.retry_after.total_seconds())
        else:
            retry_delay = compute_backoff_delay(
                attempt_number,
                base_seconds=config.retry_base_delay_seconds,
                max_seconds=config.retry_max_delay_seconds,
                rng=rng,
            )
    async with session_scope(factory) as session:
        task = await session.get(PublicationTask, task_id)
        if task is None or task.status != TaskStatus.PUBLISHING.value:
            logger.warning(
                "engine: task %s is no longer publishing; failure discarded", task_id
            )
            return
        task.status = (
            TaskStatus.FAILED.value if terminal else TaskStatus.RETRY_WAIT.value
        )
        task.claimed_at = None
        task.last_error_code = error.code
        task.last_error_message = error.message
        task.updated_at = now
        task.next_attempt_at = (
            None if terminal else now + timedelta(seconds=retry_delay or 0.0)
        )
        attempt = await _get_attempt(session, task_id, attempt_number)
        if attempt is not None and attempt.success is None:
            attempt.success = False
            attempt.finished_at = now
            attempt.error_code = error.code
            attempt.error_message = error.message
    if terminal:
        outcome.failed.append(task_id)
        logger.error(
            "engine: task %s failed permanently (attempt %d/%d, code=%s)%s",
            task_id,
            attempt_number,
            config.max_attempts,
            error.code,
            " — max attempts exhausted"
            if exhausted
            and error.classification != ErrorClassification.PERMANENT
            else "",
        )
    else:
        outcome.retried.append(task_id)
        logger.warning(
            "engine: task %s scheduled for retry (attempt %d/%d, code=%s, "
            "next_attempt_at=%s)",
            task_id,
            attempt_number,
            config.max_attempts,
            error.code,
            (now + timedelta(seconds=retry_delay or 0.0)).isoformat(),
        )


async def _get_attempt(
    session: AsyncSession, task_id: str, attempt_number: int
) -> PublicationAttempt | None:
    result = await session.execute(
        select(PublicationAttempt).where(
            PublicationAttempt.task_id == task_id,
            PublicationAttempt.attempt_number == attempt_number,
        )
    )
    return result.scalar_one_or_none()


async def _update_heartbeat(
    factory: async_sessionmaker[AsyncSession], *, now: datetime
) -> None:
    for _ in range(2):  # insert race on the singleton row: retry as an update
        try:
            async with session_scope(factory) as session:
                state = await session.get(EngineState, HEARTBEAT_ID)
                if state is None:
                    session.add(
                        EngineState(id=HEARTBEAT_ID, last_tick_at=now, updated_at=now)
                    )
                else:
                    state.last_tick_at = now
            break
        except IntegrityError:
            continue


# --------------------------------------------------------------------------
# Periodic engine service (APScheduler)
# --------------------------------------------------------------------------


class PublishingEngineService:
    """Periodic due-task processing pass backed by APScheduler.

    One interval job runs `tick()` (stale recovery, missed-slot policy, due
    publishing, heartbeat). Construction is side-effect free; `start()`
    requires a running asyncio loop. `shutdown()` stops the interval job and
    waits briefly for an in-flight tick so the process exits gracefully
    (spec §15).
    """

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        registry: AdapterRegistryLike,
        secrets: SecretProvider,
        config: EngineConfig,
        *,
        interval_seconds: int = 15,
        enabled: bool = True,
    ) -> None:
        self._factory = factory
        self._registry = registry
        self._secrets = secrets
        self._config = config
        self._interval_seconds = max(1, int(interval_seconds))
        self._enabled = enabled
        self._scheduler: AsyncIOScheduler | None = None
        self._tick_lock = asyncio.Lock()
        self.last_outcome: EngineOutcome | None = None

    async def tick(self) -> EngineOutcome:
        """Run one engine pass and remember its summary."""
        async with self._tick_lock:
            outcome = await run_engine_tick(
                self._factory, self._registry, self._secrets, self._config
            )
            self.last_outcome = outcome
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
            id="upp-publishing-engine",
            coalesce=True,
            max_instances=1,
        )
        scheduler.start()
        self._scheduler = scheduler
        logger.info("publishing engine started (interval=%ds)", self._interval_seconds)

    async def shutdown(self) -> None:
        """Stop the interval job and drain an in-flight tick (bounded)."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            logger.info("publishing engine stopped")
        if self._tick_lock.locked():
            try:
                await asyncio.wait_for(
                    self._tick_lock.acquire(), timeout=SHUTDOWN_DRAIN_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "publishing engine shutdown: in-flight tick did not finish "
                    "within %.0fs",
                    SHUTDOWN_DRAIN_SECONDS,
                )
            else:
                self._tick_lock.release()


# Re-exported so composition code can import everything engine-related from
# one module without leaking platform details into core.
__all__ = [
    "EngineConfig",
    "EngineOutcome",
    "HEARTBEAT_ID",
    "JITTER_FRACTION",
    "STALE_CLAIM_CODE",
    "PublishingEngineService",
    "claim_task",
    "compute_backoff_delay",
    "process_due_tasks",
    "recover_stale_claims",
    "reschedule_missed_slots",
    "run_engine_tick",
]
