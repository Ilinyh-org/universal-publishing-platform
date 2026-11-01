"""Publishing engine tests (roadmap step 5, specification §9.2, §9.3, §10, §17).

Covers the §17 "Publishing and reliability" items on in-memory (or
`tmp_path` file) SQLite with a fake adapter — no live platform calls, no
real secrets (AGENTS.md §5): atomic claim exclusivity, success/retry/fail
transitions, exponential backoff with jitter and its cap, exact rate-limit
delays, max attempts, stale-claim recovery with the ambiguity record, the
missed-slot downtime policy with rescheduling logs, target isolation, and
the APScheduler service plus the FastAPI lifespan wiring.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from publisher.adapters.registry import AdapterRegistry
from publisher.core.adapters import (
    AccountConfig,
    AdapterError,
    ErrorClassification,
    PublishResult,
)
from publisher.core.config import Settings
from publisher.core.content import UniversalContent
from publisher.core.publishing import (
    EngineConfig,
    EngineOutcome,
    PublishingEngineService,
    claim_task,
    compute_backoff_delay,
    process_due_tasks,
    recover_stale_claims,
    run_engine_tick,
)
from publisher.core.scheduling import assign_pending_tasks
from publisher.core.secrets import StaticSecretProvider
from publisher.db.models import (
    Account,
    EngineState,
    Platform,
    Post,
    PublicationAttempt,
    PublicationTask,
    PublishingSchedule,
    TaskStatus,
)
from publisher.db.session import create_engine as create_async_engine
from publisher.db.session import make_session_factory, session_scope
from publisher.main import create_app

NOW = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)  # Monday 06:00 UTC
CONTENT_JSON = {
    "type": "text",
    "body": "demo body",
    "format": "plain_text",
    "media": [],
}
ALL_DAYS = [1, 2, 3, 4, 5, 6, 7]
#: Next 09:00 Asia/Yekaterinburg (+5) slot after NOW: Tuesday 04:00 UTC.
NEXT_YEK_SLOT = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)

SECRETS = StaticSecretProvider(
    {"TELEGRAM_BOT_TOKEN": {"bot_token": SecretStr("test-token-not-real")}}
)


def _rng_max(a: float, b: float) -> float:
    """Deterministic rng: always the cap, so expected delays are exact."""
    return b


def _rng_record(calls: list[tuple[float, float]]) -> Callable[[float, float], float]:
    def rng(a: float, b: float) -> float:
        calls.append((a, b))
        return b

    return rng


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# --------------------------------------------------------------------------
# Fakes and seed helpers
# --------------------------------------------------------------------------


class FakeAdapter:
    """PlatformAdapter stand-in with per-account-key behavior."""

    platform_key = "telegram"
    credential_names = ("bot_token",)

    def __init__(
        self,
        default: PublishResult | Exception | None = None,
        handlers: dict[str, PublishResult | Exception | Callable[..., object]]
        | None = None,
    ) -> None:
        self.default = default if default is not None else PublishResult()
        self.handlers = handlers or {}
        self.calls: list[str] = []

    async def validate_account(self, account: AccountConfig) -> None: ...
    async def validate_content(
        self, content: object, account: AccountConfig
    ) -> None: ...
    async def publish(
        self, content: UniversalContent, account: AccountConfig
    ) -> PublishResult:
        self.calls.append(account.key)
        behavior = self.handlers.get(account.key, self.default)
        if callable(behavior):
            behavior = behavior(content, account)
        if asyncio.iscoroutine(behavior):
            behavior = await behavior
        if isinstance(behavior, Exception):
            raise behavior
        assert isinstance(behavior, PublishResult)
        return behavior


def _components(
    adapter: FakeAdapter,
    *,
    config: EngineConfig | None = None,
    registry: AdapterRegistry | None = None,
) -> tuple[AdapterRegistry, StaticSecretProvider, EngineConfig]:
    if registry is None:
        registry = AdapterRegistry()
        registry.register(adapter)
    return registry, SECRETS, config or EngineConfig()


async def _seed_account(
    factory: async_sessionmaker[AsyncSession],
    *,
    account_key: str = "english_channel",
    platform_key: str = "telegram",
    platform_enabled: bool = True,
) -> str:
    async with session_scope(factory) as session:
        platform = (
            await session.scalars(select(Platform).where(Platform.key == platform_key))
        ).first()
        if platform is None:
            platform = Platform(
                key=platform_key,
                display_name=platform_key.title(),
                enabled=platform_enabled,
            )
        platform.enabled = platform_enabled
        account = Account(
            platform=platform,
            key=account_key,
            display_name=account_key.title(),
            credentials_ref="TELEGRAM_BOT_TOKEN",
            configuration_json={"chat_id": "100"},
        )
        session.add(account)
        await session.flush()
        return account.id


_DUE = object()  # sentinel: seed a due slot (NOW - 1 minute)


async def _seed_task(
    factory: async_sessionmaker[AsyncSession],
    account_id: str,
    *,
    status: str = TaskStatus.SCHEDULED.value,
    scheduled_at: datetime | None | object = _DUE,
    next_attempt_at: datetime | None = None,
    attempt_count: int = 0,
    claimed_at: datetime | None = None,
    post_type: str = "dialogue",
) -> str:
    if scheduled_at is _DUE:
        scheduled_at = (
            NOW - timedelta(minutes=1)
            if status == TaskStatus.SCHEDULED.value
            else None
        )
    async with session_scope(factory) as session:
        post = Post(
            source="test",
            external_id=f"ext-{uuid.uuid4()}",
            content_json=dict(CONTENT_JSON),
            post_type=post_type,
        )
        task = PublicationTask(
            post=post,
            account_id=account_id,
            status=status,
            scheduled_at=scheduled_at,
            next_attempt_at=next_attempt_at,
            attempt_count=attempt_count,
            claimed_at=claimed_at,
        )
        session.add(task)
        await session.flush()
        for number in range(1, attempt_count + 1):
            session.add(
                PublicationAttempt(
                    task_id=task.id,
                    attempt_number=number,
                    started_at=NOW - timedelta(minutes=30),
                    finished_at=NOW - timedelta(minutes=29),
                    success=False,
                    error_code="network_error",
                    error_message="earlier failure",
                )
            )
        await session.flush()
        return task.id


async def _seed_schedule(
    factory: async_sessionmaker[AsyncSession],
    account_id: str,
    *,
    post_type: str = "dialogue",
) -> str:
    async with session_scope(factory) as session:
        schedule = PublishingSchedule(
            account_id=account_id,
            post_type=post_type,
            publish_time=time(9, 0),
            timezone="Asia/Yekaterinburg",
            days_of_week=ALL_DAYS,
            active=True,
        )
        session.add(schedule)
        await session.flush()
        return schedule.id


async def _seed_heartbeat(
    factory: async_sessionmaker[AsyncSession], last_tick_at: datetime | None
) -> None:
    async with session_scope(factory) as session:
        session.add(EngineState(id=1, last_tick_at=last_tick_at, updated_at=NOW))


async def _get_task(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> PublicationTask:
    async with factory() as session:
        return await session.get(PublicationTask, task_id)


async def _get_attempts(
    factory: async_sessionmaker[AsyncSession], task_id: str
) -> list[PublicationAttempt]:
    async with factory() as session:
        result = await session.execute(
            select(PublicationAttempt)
            .where(PublicationAttempt.task_id == task_id)
            .order_by(PublicationAttempt.attempt_number)
        )
        return list(result.scalars().all())


async def _get_heartbeat(
    factory: async_sessionmaker[AsyncSession],
) -> EngineState | None:
    async with factory() as session:
        return await session.get(EngineState, 1)


# --------------------------------------------------------------------------
# Backoff (spec §10)
# --------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped() -> None:
    base, cap = 30, 3600
    assert compute_backoff_delay(
        1, base_seconds=base, max_seconds=cap, rng=_rng_max
    ) == 30
    assert compute_backoff_delay(
        2, base_seconds=base, max_seconds=cap, rng=_rng_max
    ) == 60
    assert compute_backoff_delay(
        3, base_seconds=base, max_seconds=cap, rng=_rng_max
    ) == 120
    assert compute_backoff_delay(
        9, base_seconds=base, max_seconds=cap, rng=_rng_max
    ) == cap


def test_backoff_jitter_stays_in_upper_half() -> None:
    calls: list[tuple[float, float]] = []
    delay = compute_backoff_delay(
        2, base_seconds=30, max_seconds=3600, rng=_rng_record(calls)
    )
    (low, high) = calls[0]
    assert delay == high
    assert low == 30.0  # half of the exponential value (60s)
    assert high == 60.0


def test_backoff_attempt_below_one_clamped() -> None:
    assert (
        compute_backoff_delay(0, base_seconds=30, max_seconds=3600, rng=_rng_max) == 30
    )


def test_engine_config_from_settings(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None,
        max_attempts=7,
        retry_base_delay_seconds=5,
        retry_max_delay_seconds=600,
        stale_publishing_timeout_seconds=60,
    )
    assert EngineConfig.from_settings(settings) == EngineConfig(
        max_attempts=7,
        retry_base_delay_seconds=5,
        retry_max_delay_seconds=600,
        stale_publishing_timeout_seconds=60,
    )


# --------------------------------------------------------------------------
# Atomic claim (spec §9.3, §17 "Concurrent processing does not claim twice")
# --------------------------------------------------------------------------


async def test_claim_is_atomic_and_records_attempt(session_factory) -> None:
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)

    first = await claim_task(
        session_factory, task_id, expected_status=TaskStatus.SCHEDULED.value, now=NOW
    )
    assert first == 1
    second = await claim_task(
        session_factory, task_id, expected_status=TaskStatus.SCHEDULED.value, now=NOW
    )
    assert second is None  # status no longer matches: the DB-level guard

    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHING.value
    assert task.claimed_at is not None
    assert task.attempt_count == 1
    attempts = await _get_attempts(session_factory, task_id)
    assert [a.attempt_number for a in attempts] == [1]
    assert attempts[0].success is None  # in-flight attempt


async def test_parallel_ticks_never_claim_twice(tmp_path) -> None:
    """Two simultaneous engine passes over the same due task publish it once."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/conc.db")
    try:
        async with engine.begin() as conn:
            from publisher.db.models import Base

            await conn.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        account_id = await _seed_account(factory)
        task_id = await _seed_task(factory, account_id)
        await _seed_heartbeat(factory, NOW - timedelta(seconds=1))

        adapter = FakeAdapter()

        async def sleep_then_publish(content: object, account: object) -> PublishResult:
            await asyncio.sleep(0.05)
            return PublishResult(external_post_id="msg-1")

        adapter.default = sleep_then_publish
        registry, secrets, config = _components(adapter)

        outcomes = await asyncio.gather(
            run_engine_tick(factory, registry, secrets, config, now=NOW),
            run_engine_tick(factory, registry, secrets, config, now=NOW),
        )
        published = [task_id for outcome in outcomes for task_id in outcome.published]
        assert sorted(published) == [task_id]
        assert adapter.calls.count("english_channel") == 1
        task = await _get_task(factory, task_id)
        assert task.status == TaskStatus.PUBLISHED.value
        assert task.attempt_count == 1
        assert len(await _get_attempts(factory, task_id)) == 1
    finally:
        await engine.dispose()


# --------------------------------------------------------------------------
# Success / retry / fail transitions (spec §9.3)
# --------------------------------------------------------------------------


async def test_success_records_external_id_published_at_and_attempt(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=PublishResult(external_post_id="msg-42", metadata={"message_id": 42})
    )
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.published == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHED.value
    assert task.external_post_id == "msg-42"
    assert _utc(task.published_at) == NOW
    assert task.claimed_at is None
    assert task.next_attempt_at is None
    assert task.last_error_code is None
    attempts = await _get_attempts(session_factory, task_id)
    assert len(attempts) == 1
    assert attempts[0].success is True
    assert attempts[0].finished_at is not None
    assert attempts[0].response_metadata_json == {"message_id": 42}


async def test_temporary_failure_schedules_retry_with_backoff(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=AdapterError(
            "network_error",
            "Telegram request failed: ConnectError",
            ErrorClassification.TEMPORARY,
        )
    )
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    assert outcome.retried == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.RETRY_WAIT.value
    assert _utc(task.next_attempt_at) == NOW + timedelta(seconds=30)
    assert task.claimed_at is None
    assert task.last_error_code == "network_error"
    attempts = await _get_attempts(session_factory, task_id)
    assert attempts[0].success is False
    assert attempts[0].error_code == "network_error"
    assert attempts[0].finished_at is not None


async def test_backoff_grows_with_attempts_and_respects_cap(session_factory):
    account_id = await _seed_account(session_factory)
    # Three failed attempts already: the next one is attempt 4 (8 * base).
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.RETRY_WAIT.value,
        next_attempt_at=NOW - timedelta(minutes=1),
        attempt_count=3,
    )
    adapter = FakeAdapter(
        default=AdapterError(
            "network_error", "still down", ErrorClassification.TEMPORARY
        )
    )
    registry, secrets, config = _components(
        adapter, config=EngineConfig(retry_base_delay_seconds=30, retry_max_delay_seconds=100)
    )

    await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.RETRY_WAIT.value
    # 30 * 2^3 = 240 would exceed the cap; 100 must win (spec §10).
    assert _utc(task.next_attempt_at) == NOW + timedelta(seconds=100)


async def test_rate_limit_delay_respected_exactly(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=AdapterError(
            "rate_limited",
            "Telegram rate limit: retry after 120",
            ErrorClassification.RATE_LIMITED,
            retry_after=timedelta(seconds=120),
        )
    )
    registry, secrets, config = _components(adapter)

    # Even a maximal jitter must not shorten the platform-provided delay.
    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    assert outcome.retried == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.RETRY_WAIT.value
    assert _utc(task.next_attempt_at) == NOW + timedelta(seconds=120)
    assert task.last_error_code == "rate_limited"


async def test_rate_limit_without_retry_after_uses_backoff(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=AdapterError(
            "rate_limited", "rate limited", ErrorClassification.RATE_LIMITED
        )
    )
    registry, secrets, config = _components(adapter)

    await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    task = await _get_task(session_factory, task_id)
    assert _utc(task.next_attempt_at) == NOW + timedelta(seconds=30)


async def test_permanent_failure_fails_immediately(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=AdapterError(
            "invalid_credentials",
            "Telegram rejected the bot token (401 Unauthorized)",
            ErrorClassification.PERMANENT,
        )
    )
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.failed == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.FAILED.value
    assert task.next_attempt_at is None
    assert task.attempt_count == 1
    assert task.last_error_code == "invalid_credentials"
    assert (await _get_attempts(session_factory, task_id))[0].success is False


async def test_max_attempts_enforced(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(
        default=AdapterError(
            "network_error", "flaky", ErrorClassification.TEMPORARY
        )
    )
    registry, secrets, config = _components(
        adapter, config=EngineConfig(max_attempts=3, retry_base_delay_seconds=30)
    )

    now = NOW
    outcomes: list[EngineOutcome] = []
    for _ in range(5):  # more ticks than allowed attempts
        outcome = await process_due_tasks(
            session_factory, registry, secrets, config, now=now, rng=_rng_max
        )
        outcomes.append(outcome)
        now += timedelta(hours=1)  # clear the exponential backoff every time

    assert [o.retried for o in outcomes] == [[task_id], [task_id], [], [], []]
    assert outcomes[2].failed == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.FAILED.value
    assert task.attempt_count == 3
    assert len(await _get_attempts(session_factory, task_id)) == 3


async def test_success_on_retry_clears_error_fields(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.RETRY_WAIT.value,
        next_attempt_at=NOW - timedelta(minutes=1),
        attempt_count=1,
    )
    adapter = FakeAdapter(default=PublishResult(external_post_id="msg-7"))
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.published == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHED.value
    assert task.external_post_id == "msg-7"
    assert task.attempt_count == 2
    assert task.last_error_code is None
    assert task.next_attempt_at is None
    attempts = await _get_attempts(session_factory, task_id)
    assert [(a.attempt_number, a.success) for a in attempts] == [(1, False), (2, True)]


async def test_unexpected_exception_is_retried_and_sanitized(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)

    class ExplodingAdapter(FakeAdapter):
        async def publish(self, content, account):  # type: ignore[override]
            self.calls.append(account.key)
            raise RuntimeError("boom with SECRET-DATA")

    adapter = ExplodingAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    assert outcome.retried == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.RETRY_WAIT.value
    assert task.last_error_code == "unexpected_error"
    assert "SECRET-DATA" not in (task.last_error_message or "")
    assert task.last_error_message == "unexpected failure: RuntimeError"
    attempt = (await _get_attempts(session_factory, task_id))[0]
    assert "SECRET-DATA" not in (attempt.error_message or "")


# --------------------------------------------------------------------------
# Claim exclusivity and due selection
# --------------------------------------------------------------------------


async def test_processed_task_is_not_processed_again(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter(default=PublishResult(external_post_id="msg-1"))
    registry, secrets, config = _components(adapter)

    first = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )
    second = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert first.published == [task_id]
    assert second == EngineOutcome()
    assert adapter.calls == ["english_channel"]
    assert len(await _get_attempts(session_factory, task_id)) == 1


async def test_fresh_publishing_claim_is_left_alone(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.PUBLISHING.value,
        claimed_at=NOW - timedelta(seconds=10),
        attempt_count=1,
    )
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await run_engine_tick(session_factory, registry, secrets, config, now=NOW)

    assert outcome.stale_recovered == []
    assert outcome.published == []
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHING.value


async def test_not_due_tasks_are_untouched(session_factory):
    account_id = await _seed_account(session_factory)
    future_task = await _seed_task(
        session_factory, account_id, scheduled_at=NOW + timedelta(hours=1)
    )
    retry_task = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.RETRY_WAIT.value,
        next_attempt_at=NOW + timedelta(hours=1),
        attempt_count=1,
    )
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome == EngineOutcome()
    assert adapter.calls == []
    assert (await _get_task(session_factory, future_task)).status == "scheduled"
    assert (await _get_task(session_factory, retry_task)).status == "retry_wait"


async def test_task_on_unknown_platform_is_skipped(session_factory):
    account_id = await _seed_account(session_factory, platform_key="mastodon")
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)  # telegram only: mastodon has no adapter
    _, secrets, config = _components(adapter, registry=registry)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.published == []
    assert "no adapter registered for platform 'mastodon'" in outcome.skipped[task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.SCHEDULED.value  # left due, never claimed
    assert task.attempt_count == 0


async def test_task_on_disabled_platform_is_skipped(session_factory):
    account_id = await _seed_account(session_factory, platform_enabled=False)
    task_id = await _seed_task(session_factory, account_id)
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.skipped[task_id] == "platform unknown or disabled"
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.SCHEDULED.value
    assert adapter.calls == []


# --------------------------------------------------------------------------
# Stale-claim recovery (spec §9.3, §17)
# --------------------------------------------------------------------------


async def test_stale_claim_recovered_to_retry_queue(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.PUBLISHING.value,
        claimed_at=NOW - timedelta(seconds=1800),
        attempt_count=1,
    )
    # Rewrite the pre-seeded failed attempt row to the unfinished in-flight one.
    async with session_scope(session_factory) as session:
        attempt = (
            await session.scalars(
                select(PublicationAttempt).where(PublicationAttempt.task_id == task_id)
            )
        ).one()
        attempt.success = None
        attempt.finished_at = None
        attempt.error_code = None
        attempt.error_message = None

    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter, config=EngineConfig())

    outcome = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )

    assert outcome.stale_recovered == [task_id]
    assert adapter.calls == []  # recovery never publishes
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.RETRY_WAIT.value
    assert task.claimed_at is None
    assert _utc(task.next_attempt_at) == NOW + timedelta(seconds=30)
    assert task.last_error_code == "stale_claim"
    attempt = (await _get_attempts(session_factory, task_id))[0]
    assert attempt.success is False
    assert attempt.error_code == "stale_claim"
    assert "duplicate" in (attempt.error_message or "")


async def test_stale_claim_at_max_attempts_fails(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.PUBLISHING.value,
        claimed_at=NOW - timedelta(seconds=1800),
        attempt_count=5,
    )

    recovered = await recover_stale_claims(
        session_factory, EngineConfig(max_attempts=5), now=NOW
    )

    assert recovered == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.FAILED.value
    assert task.next_attempt_at is None
    assert task.last_error_code == "stale_claim"


async def test_recovered_task_republishes_after_backoff(session_factory):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory,
        account_id,
        status=TaskStatus.PUBLISHING.value,
        claimed_at=NOW - timedelta(seconds=1800),
        attempt_count=1,
    )
    adapter = FakeAdapter(default=PublishResult(external_post_id="msg-9"))
    registry, secrets, config = _components(
        adapter, config=EngineConfig(retry_base_delay_seconds=30)
    )

    first = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW, rng=_rng_max
    )
    assert first.stale_recovered == [task_id]

    second = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW + timedelta(seconds=60)
    )
    assert second.published == [task_id]
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHED.value
    assert task.external_post_id == "msg-9"


# --------------------------------------------------------------------------
# Target isolation (§17 "one target failing does not affect another")
# --------------------------------------------------------------------------


async def test_one_target_failing_does_not_affect_another(session_factory):
    good_account = await _seed_account(session_factory, account_key="good_channel")
    bad_account = await _seed_account(session_factory, account_key="bad_channel")
    good_task = await _seed_task(session_factory, good_account)
    bad_task = await _seed_task(session_factory, bad_account)
    adapter = FakeAdapter(
        handlers={
            "good_channel": PublishResult(external_post_id="ok-1"),
            "bad_channel": AdapterError(
                "invalid_destination",
                "Telegram destination problem: chat not found",
                ErrorClassification.PERMANENT,
            ),
        }
    )
    registry, secrets, config = _components(adapter)

    outcome = await process_due_tasks(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.published == [good_task]
    assert outcome.failed == [bad_task]
    good = await _get_task(session_factory, good_task)
    bad = await _get_task(session_factory, bad_task)
    assert good.status == TaskStatus.PUBLISHED.value
    assert good.external_post_id == "ok-1"
    assert bad.status == TaskStatus.FAILED.value


# --------------------------------------------------------------------------
# Missed-slot downtime policy (spec §9.2)
# --------------------------------------------------------------------------


async def test_missed_slots_are_rescheduled_after_downtime(session_factory, caplog):
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    task_id = await _seed_task(
        session_factory,
        account_id,
        scheduled_at=NOW - timedelta(minutes=10),  # missed during downtime
    )
    await _seed_heartbeat(session_factory, NOW - timedelta(hours=1))  # gap > timeout
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    with caplog.at_level(logging.INFO, logger="publisher.core.publishing"):
        outcome = await run_engine_tick(
            session_factory, registry, secrets, config, now=NOW
        )

    assert outcome.rescheduled == [task_id]
    assert outcome.published == []
    assert adapter.calls == []  # no backlog burst
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.SCHEDULED.value
    assert _utc(task.scheduled_at) == NEXT_YEK_SLOT
    assert any(
        "rescheduled" in record.getMessage() and task_id in record.getMessage()
        for record in caplog.records
    )


async def test_relocation_respects_slot_exclusivity(session_factory):
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    other_task = await _seed_task(
        session_factory, account_id, scheduled_at=NEXT_YEK_SLOT  # occupies Tue slot
    )
    missed_task = await _seed_task(
        session_factory, account_id, scheduled_at=NOW - timedelta(minutes=10)
    )
    await _seed_heartbeat(session_factory, NOW - timedelta(hours=1))
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.rescheduled == [missed_task]
    moved = await _get_task(session_factory, missed_task)
    # The Tuesday slot is taken by `other_task`; the next free slot is Wednesday.
    assert _utc(moved.scheduled_at) == NEXT_YEK_SLOT + timedelta(days=1)
    keeper = await _get_task(session_factory, other_task)
    assert _utc(keeper.scheduled_at) == NEXT_YEK_SLOT  # untouched


async def test_running_engine_publishes_due_tasks_normally(session_factory):
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    task_id = await _seed_task(
        session_factory, account_id, scheduled_at=NOW - timedelta(seconds=5)
    )
    await _seed_heartbeat(session_factory, NOW - timedelta(seconds=15))  # gap <= timeout
    adapter = FakeAdapter(default=PublishResult(external_post_id="msg-2"))
    registry, secrets, config = _components(adapter)

    outcome = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.rescheduled == []
    assert outcome.published == [task_id]  # normal operation, no downtime policy
    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.PUBLISHED.value


async def test_first_tick_ever_applies_downtime_policy(session_factory):
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    task_id = await _seed_task(
        session_factory, account_id, scheduled_at=NOW - timedelta(minutes=5)
    )
    # No engine_state row at all: the engine could have been off for a day.
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)

    outcome = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW
    )

    assert outcome.rescheduled == [task_id]
    assert outcome.published == []
    task = await _get_task(session_factory, task_id)
    assert _utc(task.scheduled_at) == NEXT_YEK_SLOT


async def test_missed_slot_without_schedule_stays_due_and_publishes(
    session_factory, caplog
):
    account_id = await _seed_account(session_factory)
    task_id = await _seed_task(
        session_factory, account_id, scheduled_at=NOW - timedelta(minutes=5)
    )
    await _seed_heartbeat(session_factory, NOW - timedelta(hours=1))
    adapter = FakeAdapter(default=PublishResult(external_post_id="msg-3"))
    registry, secrets, config = _components(adapter)

    with caplog.at_level(logging.WARNING, logger="publisher.core.publishing"):
        outcome = await run_engine_tick(
            session_factory, registry, secrets, config, now=NOW
        )

    assert outcome.missed_kept == [task_id]
    assert outcome.published == [task_id]  # documented fallback: no burst target
    assert any("no usable active schedule" in r.getMessage() for r in caplog.records)


async def test_pending_unscheduled_work_moves_to_future_slots(session_factory):
    """§9.2: unscheduled pending work finds future slots (never missed ones).

    Mirrors the deployed composition: the SchedulerService assignment pass
    assigns future slots to unscheduled pending tasks; the engine tick then
    applies the downtime policy and publishes what is due.
    """
    account_id = await _seed_account(session_factory)
    await _seed_schedule(session_factory, account_id)
    task_id = await _seed_task(
        session_factory, account_id, status=TaskStatus.PENDING.value
    )
    await _seed_heartbeat(session_factory, NOW - timedelta(hours=1))
    registry, secrets, config = _components(FakeAdapter())

    await assign_pending_tasks(session_factory, now=NOW)  # SchedulerService pass
    outcome = await run_engine_tick(
        session_factory, registry, secrets, config, now=NOW
    )

    task = await _get_task(session_factory, task_id)
    assert task.status == TaskStatus.SCHEDULED.value
    assert _utc(task.scheduled_at) == NEXT_YEK_SLOT  # a future slot, never the past
    assert outcome.published == []


# --------------------------------------------------------------------------
# Heartbeat and service (spec §15/§16)
# --------------------------------------------------------------------------


async def test_tick_records_heartbeat(session_factory):
    registry, secrets, config = _components(FakeAdapter())
    await run_engine_tick(session_factory, registry, secrets, config, now=NOW)
    heartbeat = await _get_heartbeat(session_factory)
    assert heartbeat is not None
    assert heartbeat.last_tick_at is not None
    assert _utc(heartbeat.last_tick_at) == NOW


async def test_engine_service_tick_start_and_shutdown(session_factory):
    adapter = FakeAdapter()
    registry, secrets, config = _components(adapter)
    service = PublishingEngineService(
        session_factory, registry, secrets, config, interval_seconds=15
    )
    assert service.last_outcome is None

    outcome = await service.tick()
    assert isinstance(outcome, EngineOutcome)
    assert service.last_outcome is outcome
    assert (await _get_heartbeat(session_factory)) is not None

    service.start()
    assert service._scheduler is not None
    assert [job.id for job in service._scheduler.get_jobs()] == ["upp-publishing-engine"]
    service.start()  # idempotent
    assert len(service._scheduler.get_jobs()) == 1

    await service.shutdown()
    assert service._scheduler is None


async def test_engine_service_disabled_never_starts(session_factory):
    registry, secrets, config = _components(FakeAdapter())
    service = PublishingEngineService(
        session_factory, registry, secrets, config, interval_seconds=15, enabled=False
    )
    service.start()
    assert service._scheduler is None


# --------------------------------------------------------------------------
# FastAPI lifespan wiring (roadmap step 5)
# --------------------------------------------------------------------------


def _lifespan_engine(tmp_path) -> AsyncEngine:
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/lifespan.db")


def _lifespan_settings(database_url: str) -> Settings:
    return Settings(
        app_env="test",
        api_key="test-key",
        scheduler_enabled=True,
        engine_enabled=True,
        scheduler_poll_interval_seconds=3600,  # no tick can fire mid-test
        database_url=database_url,
    )


def test_lifespan_starts_and_stops_background_services(tmp_path):
    from publisher.db.models import Base

    async def _create() -> AsyncEngine:
        engine = _lifespan_engine(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    engine = asyncio.run(_create())
    app = create_app(settings=_lifespan_settings(str(engine.url)), engine=engine)
    try:
        with TestClient(app) as client:
            publishing = app.state.publishing_service
            scheduler = app.state.scheduler_service
            assert publishing._scheduler is not None
            assert scheduler._scheduler is not None
            assert client.get("/health").status_code == 200
        # Graceful shutdown: both interval jobs stopped with the lifespan.
        assert app.state.publishing_service._scheduler is None
        assert app.state.scheduler_service._scheduler is None
    finally:
        asyncio.run(engine.dispose())


def test_lifespan_without_engine_flag_stays_deterministic(tmp_path):
    from publisher.db.models import Base

    async def _create() -> AsyncEngine:
        engine = _lifespan_engine(tmp_path)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    engine = asyncio.run(_create())
    settings = _lifespan_settings(str(engine.url))
    settings.engine_enabled = False
    app = create_app(settings=settings, engine=engine)
    try:
        with TestClient(app) as client:
            assert getattr(app.state, "publishing_service", None) is None
            assert getattr(app.state, "scheduler_service", None) is None
            assert client.get("/health").status_code == 200
    finally:
        asyncio.run(engine.dispose())
