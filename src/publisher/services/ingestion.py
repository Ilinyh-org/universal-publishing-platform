"""Batch ingestion service (specification §6, architecture §3.1).

Processes each submitted item independently in its own transaction: an
accepted item's post and publication tasks are committed atomically before
it is reported `accepted`, and one item's failure never rolls back another
(spec §6.1, architecture §8).

Idempotency (spec §6.5): the unique key is `(source, external_id)`, enforced
by a database constraint, not only application checks. A repeated key with
identical immutable data is a `duplicate` no-op; a repeated key with
different immutable data is a `conflict` that never overwrites the original.

Immutable comparison set for version 1 — treated as strictly immutable:
`content`, `post_type`, the normalized target set, `metadata`, and
`created_at`. The target set compared against an existing post is the
persisted set of valid targets (invalid targets are never persisted), with
duplicate targets within a request collapsed to one.

Target resolution (spec §6.4) resolves `(platform, account)` against the
`platforms`/`accounts` tables: the platform row must exist and be enabled,
the account must exist under that platform and be active. (When the adapter
registry lands with the Telegram adapter, adapter availability adds one more
resolution check; the data-model rules below are unchanged.) Invalid targets
are reported per item and never create tasks; an item is accepted only if at
least one target resolves.

Result models are plain pydantic so the REST layer (roadmap step 3) can
return them as the itemized batch response (spec §6.6) without translation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..core.content import PostBatchRequest, PostSubmission
from ..db.models import Account, Platform, Post, PublicationTask, TaskStatus
from ..db.session import session_scope


class ItemStatus(StrEnum):
    """Per-item ingestion outcome (spec §6.6)."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    REJECTED = "rejected"


class InvalidTarget(BaseModel):
    """A target that did not resolve, with a stable error code (spec §6.6)."""

    platform: str
    account: str
    code: str
    message: str


class ItemResult(BaseModel):
    """Itemized result for one submitted post (spec §6.6)."""

    external_id: str
    status: ItemStatus
    post_id: str | None = None
    created_tasks: int = 0
    invalid_targets: list[InvalidTarget] = Field(default_factory=list)
    code: str | None = None
    message: str | None = None


class BatchIngestionResult(BaseModel):
    """Batch envelope result: one entry per submitted item (spec §6.6)."""

    source: str
    results: list[ItemResult]


class IngestionError(Exception):
    """Stable-code ingestion failure that is not a per-item outcome."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _normalize_metadata(metadata: dict | None) -> dict | None:
    """`{}` and `null` are the same: no metadata."""
    return metadata or None


def _as_utc(value: datetime) -> datetime:
    """Normalize an instant to UTC (spec §8: UTC persistence).

    SQLite stores `DateTime(timezone=True)` without an offset marker, so
    values read back are naive; per the storage convention they are UTC.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _content_json(item: PostSubmission) -> dict:
    """Normalized universal content, preserved as received (spec §6.3)."""
    return item.content.model_dump(mode="json")


def _target_set(item: PostSubmission) -> list[tuple[str, str]]:
    """Normalized target set: distinct `(platform, account)` pairs, sorted."""
    return sorted({(t.platform, t.account) for t in item.targets})


async def _resolve_targets(
    session: AsyncSession, item: PostSubmission
) -> tuple[list[Account], list[InvalidTarget]]:
    """Resolve each target against platforms/accounts (spec §6.4)."""
    valid: list[Account] = []
    invalid: list[InvalidTarget] = []
    seen: set[tuple[str, str]] = set()
    for target in item.targets:
        pair = (target.platform, target.account)
        if pair in seen:
            continue
        seen.add(pair)
        row = (
            await session.execute(
                select(Account, Platform)
                .join(Platform, Platform.id == Account.platform_id)
                .where(Platform.key == target.platform, Account.key == target.account)
            )
        ).first()
        if row is None:
            platform_exists = (
                await session.execute(
                    select(Platform.enabled).where(Platform.key == target.platform)
                )
            ).scalar_one_or_none()
            if platform_exists is None:
                invalid.append(
                    InvalidTarget(
                        platform=target.platform,
                        account=target.account,
                        code="unknown_platform",
                        message=f"platform '{target.platform}' is not registered",
                    )
                )
            elif not platform_exists:
                invalid.append(
                    InvalidTarget(
                        platform=target.platform,
                        account=target.account,
                        code="platform_disabled",
                        message=f"platform '{target.platform}' is disabled",
                    )
                )
            else:
                invalid.append(
                    InvalidTarget(
                        platform=target.platform,
                        account=target.account,
                        code="unknown_account",
                        message=(
                            f"account '{target.account}' does not exist for "
                            f"platform '{target.platform}'"
                        ),
                    )
                )
            continue
        account, platform = row
        if not platform.enabled:
            invalid.append(
                InvalidTarget(
                    platform=target.platform,
                    account=target.account,
                    code="platform_disabled",
                    message=f"platform '{target.platform}' is disabled",
                )
            )
        elif not account.active:
            invalid.append(
                InvalidTarget(
                    platform=target.platform,
                    account=target.account,
                    code="inactive_account",
                    message=f"account '{target.account}' is inactive",
                )
            )
        else:
            valid.append(account)
    return valid, invalid


async def _persisted_target_set(session: AsyncSession, post_id: str) -> list[tuple[str, str]]:
    """The post's persisted (valid) targets as a sorted distinct pair list."""
    rows = await session.execute(
        select(Platform.key, Account.key)
        .join(Account, Account.platform_id == Platform.id)
        .join(PublicationTask, PublicationTask.account_id == Account.id)
        .where(PublicationTask.post_id == post_id)
    )
    return sorted(set(rows.all()))


def _immutable_diff(post: Post, item: PostSubmission, targets: list[tuple[str, str]]) -> list[str]:
    """Names of immutable fields that differ between the stored post and `item`."""
    diffs: list[str] = []
    if post.content_json != _content_json(item):
        diffs.append("content")
    if post.post_type != item.post_type:
        diffs.append("post_type")
    if targets != _target_set(item):
        diffs.append("targets")
    if post.metadata_json != _normalize_metadata(item.metadata):
        diffs.append("metadata")
    stored_created = _as_utc(post.source_created_at) if post.source_created_at else None
    incoming_created = _as_utc(item.created_at) if item.created_at else None
    if stored_created != incoming_created:
        diffs.append("created_at")
    return diffs


async def _classify_existing(
    factory: async_sessionmaker[AsyncSession],
    source: str,
    item: PostSubmission,
) -> ItemResult:
    """Read-only duplicate/conflict classification.

    Used after an `IntegrityError`: the database constraint is the final
    authority on `(source, external_id)` uniqueness, so a racer that lost
    the insert is classified against the winner's data.
    """
    async with factory() as session:
        post = (
            await session.execute(
                select(Post).where(Post.source == source, Post.external_id == item.external_id)
            )
        ).scalar_one_or_none()
        if post is None:
            raise IngestionError(
                "ingestion_race_lost",
                f"post '{item.external_id}' disappeared after an integrity error",
            )
        targets = await _persisted_target_set(session, post.id)
        diffs = _immutable_diff(post, item, targets)
        if diffs:
            return ItemResult(
                external_id=item.external_id,
                status=ItemStatus.CONFLICT,
                post_id=post.id,
                code="conflict",
                message=f"immutable fields differ from the stored post: {', '.join(diffs)}",
            )
        return ItemResult(
            external_id=item.external_id,
            status=ItemStatus.DUPLICATE,
            post_id=post.id,
            code="duplicate",
            message="already accepted; no changes made",
        )


async def _ingest_item(
    factory: async_sessionmaker[AsyncSession],
    source: str,
    item: PostSubmission,
) -> ItemResult:
    """Ingest one item atomically: post + one task per valid target (spec §6.1)."""
    async with session_scope(factory) as session:
        existing = (
            await session.execute(
                select(Post).where(Post.source == source, Post.external_id == item.external_id)
            )
        ).scalar_one_or_none()
        if existing is not None:
            targets = await _persisted_target_set(session, existing.id)
            diffs = _immutable_diff(existing, item, targets)
            if diffs:
                return ItemResult(
                    external_id=item.external_id,
                    status=ItemStatus.CONFLICT,
                    post_id=existing.id,
                    code="conflict",
                    message=f"immutable fields differ from the stored post: {', '.join(diffs)}",
                )
            return ItemResult(
                external_id=item.external_id,
                status=ItemStatus.DUPLICATE,
                post_id=existing.id,
                code="duplicate",
                message="already accepted; no changes made",
            )

        accounts, invalid_targets = await _resolve_targets(session, item)
        if not accounts:
            return ItemResult(
                external_id=item.external_id,
                status=ItemStatus.REJECTED,
                created_tasks=0,
                invalid_targets=invalid_targets,
                code="no_valid_targets",
                message="no target resolved; nothing was stored",
            )

        post = Post(
            source=source,
            external_id=item.external_id,
            content_json=_content_json(item),
            post_type=item.post_type,
            metadata_json=_normalize_metadata(item.metadata),
            source_created_at=_as_utc(item.created_at) if item.created_at else None,
        )
        session.add(post)
        for account in accounts:
            session.add(
                PublicationTask(post=post, account_id=account.id, status=TaskStatus.PENDING)
            )
        await session.flush()
        return ItemResult(
            external_id=item.external_id,
            status=ItemStatus.ACCEPTED,
            post_id=post.id,
            created_tasks=len(accounts),
            invalid_targets=invalid_targets,
        )


async def ingest_batch(
    factory: async_sessionmaker[AsyncSession],
    batch: PostBatchRequest,
) -> BatchIngestionResult:
    """Ingest a validated batch, one independent transaction per item."""
    results: list[ItemResult] = []
    for item in batch.posts:
        try:
            results.append(await _ingest_item(factory, batch.source, item))
        except IntegrityError:
            # The `(source, external_id)` constraint decided: classify
            # against the stored winner instead of surfacing a DB error.
            results.append(await _classify_existing(factory, batch.source, item))
    return BatchIngestionResult(source=batch.source, results=results)
