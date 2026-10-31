"""Ingestion service tests on SQLite (specification §6, §17 ingestion items).

No HTTP layer here (roadmap step 3): the batch envelope is validated by the
step-1 pydantic models and handed straight to `ingest_batch`. Batch-size
boundaries enforced by the envelope are covered in `test_content.py`; these
tests cover per-item processing, idempotency/conflict detection, target
resolution, and persistence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from publisher.core.content import MediaItem, PostBatchRequest, PostSubmission, UniversalContent
from publisher.db.demo import populate_demo_data
from publisher.db.models import Account, Base, Platform, Post, PublicationTask
from publisher.db.session import create_engine as create_async_engine
from publisher.db.session import make_session_factory
from publisher.services.ingestion import ItemStatus, ingest_batch


def _post(
    external_id: str,
    *,
    body: str = "Can I have a coffee?\nSure!",
    post_type: str = "dialogue",
    targets: list[tuple[str, str]] | None = None,
    metadata: dict | None = None,
    created_at: datetime | None = None,
    title: str | None = "At the coffee shop",
) -> PostSubmission:
    return PostSubmission(
        external_id=external_id,
        content=UniversalContent(type="text", title=title, body=body, media=[]),
        targets=[
            {"platform": platform, "account": account}
            for platform, account in (targets or [("telegram", "english_channel")])
        ],
        post_type=post_type,
        metadata=metadata,
        created_at=created_at,
    )


async def _seed_telegram_account(
    factory: async_sessionmaker, *, account_active: bool = True, platform_enabled: bool = True
) -> Account:
    async with factory() as session:
        platform = Platform(key="telegram", display_name="Telegram", enabled=platform_enabled)
        account = Account(
            platform=platform,
            key="english_channel",
            display_name="English A1",
            credentials_ref="TELEGRAM_BOT_TOKEN",
            active=account_active,
        )
        session.add_all([platform, account])
        await session.commit()
        return account


async def _counts(factory: async_sessionmaker) -> tuple[int, int]:
    async with factory() as session:
        posts = (await session.execute(select(func.count(Post.id)))).scalar_one()
        tasks = (await session.execute(select(func.count(PublicationTask.id)))).scalar_one()
    return posts, tasks


async def _task_for(factory: async_sessionmaker, post_id: str) -> PublicationTask:
    async with factory() as session:
        task = (
            await session.execute(select(PublicationTask).where(PublicationTask.post_id == post_id))
        ).scalar_one()
        session.expunge(task)
        return task


async def test_single_post_accepted_creates_post_and_pending_task(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    created_at = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    batch = PostBatchRequest(
        source="n8n",
        posts=[
            _post("english-dialogue-000125", metadata={"language": "en"}, created_at=created_at)
        ],
    )

    result = await ingest_batch(session_factory, batch)

    assert result.source == "n8n"
    assert len(result.results) == 1
    item = result.results[0]
    assert item.status is ItemStatus.ACCEPTED
    assert item.post_id is not None
    assert item.created_tasks == 1
    assert item.invalid_targets == []

    task = await _task_for(session_factory, item.post_id)
    assert task.status == "pending"
    assert task.account_id is not None
    assert task.attempt_count == 0
    assert task.scheduled_at is None

    async with session_factory() as session:
        post = await session.get(Post, item.post_id)
        assert post is not None
        assert post.source == "n8n"
        assert post.external_id == "english-dialogue-000125"
        # Original universal content preserved as received (spec 6.3).
        assert post.content_json == {
            "type": "text",
            "title": "At the coffee shop",
            "body": "Can I have a coffee?\nSure!",
            "format": "plain_text",
            "media": [],
        }
        assert post.post_type == "dialogue"
        assert post.metadata_json == {"language": "en"}
        # SQLite stores DateTime(timezone=True) without an offset; the
        # storage convention is UTC (see ingestion._as_utc).
        assert post.source_created_at.replace(tzinfo=UTC) == created_at


async def test_batch_of_ten_posts_accepted(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    posts = [_post(f"post-{i:03d}", body=f"body {i}") for i in range(10)]
    batch = PostBatchRequest(source="n8n", posts=posts)

    result = await ingest_batch(session_factory, batch)

    assert [r.status for r in result.results] == [ItemStatus.ACCEPTED] * 10
    assert await _counts(session_factory) == (10, 10)


async def test_duplicate_submission_is_a_noop(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    batch = PostBatchRequest(source="n8n", posts=[_post("english-dialogue-000125")])

    first = await ingest_batch(session_factory, batch)
    second = await ingest_batch(session_factory, batch)

    dup = second.results[0]
    assert dup.status is ItemStatus.DUPLICATE
    assert dup.post_id == first.results[0].post_id
    assert dup.created_tasks == 0
    assert await _counts(session_factory) == (1, 1)
    # Re-sending must not reset task state (spec 6.5).
    task = await _task_for(session_factory, first.results[0].post_id)
    assert task.status == "pending"
    assert task.attempt_count == 0


async def test_conflicting_duplicate_payload_is_reported(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    key = "english-dialogue-000125"
    await ingest_batch(session_factory, PostBatchRequest(source="n8n", posts=[_post(key)]))

    result = await ingest_batch(
        session_factory,
        PostBatchRequest(source="n8n", posts=[_post(key, body="A different body")]),
    )

    conflict = result.results[0]
    assert conflict.status is ItemStatus.CONFLICT
    assert conflict.code == "conflict"
    assert conflict.message is not None and "content" in conflict.message
    # The original post is never overwritten (spec 6.5).
    async with session_factory() as session:
        post = (await session.execute(select(Post).where(Post.external_id == key))).scalar_one()
        assert post.content_json["body"] == "Can I have a coffee?\nSure!"
    assert await _counts(session_factory) == (1, 1)


async def test_invalid_items_do_not_block_valid_ones(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    batch = PostBatchRequest(
        source="n8n",
        posts=[
            _post("good-post"),
            _post("orphan-post", targets=[("mastodon", "some_account")]),
        ],
    )

    result = await ingest_batch(session_factory, batch)

    assert [r.status for r in result.results] == [ItemStatus.ACCEPTED, ItemStatus.REJECTED]
    rejected = result.results[1]
    assert rejected.code == "no_valid_targets"
    assert rejected.invalid_targets[0].code == "unknown_platform"
    assert await _counts(session_factory) == (1, 1)


async def test_mixed_targets_keep_valid_and_report_invalid(session_factory) -> None:
    account = await _seed_telegram_account(session_factory)
    batch = PostBatchRequest(
        source="n8n",
        posts=[
            _post(
                "mixed-targets",
                targets=[("telegram", "english_channel"), ("telegram", "missing_channel")],
            )
        ],
    )

    result = await ingest_batch(session_factory, batch)

    item = result.results[0]
    assert item.status is ItemStatus.ACCEPTED
    assert item.created_tasks == 1
    assert len(item.invalid_targets) == 1
    assert item.invalid_targets[0].code == "unknown_account"
    assert item.invalid_targets[0].account == "missing_channel"
    task = await _task_for(session_factory, item.post_id)
    assert task.account_id == account.id


async def test_target_resolution_error_codes(session_factory) -> None:
    async with session_factory() as session:
        rss = Platform(key="rss", display_name="RSS", enabled=False)
        listserv_platform = Platform(key="email", display_name="Email", enabled=True)
        # A registered platform with no accounts, for the unknown-account case.
        telegram = Platform(key="telegram", display_name="Telegram", enabled=True)
        session.add_all([rss, listserv_platform, telegram])
        await session.flush()
        session.add_all(
            [
                # Active account on a disabled platform -> platform_disabled.
                Account(
                    platform_id=rss.id,
                    key="news_feed",
                    display_name="News",
                    credentials_ref="RSS_SECRET",
                    active=True,
                ),
                # Inactive account on an enabled platform -> inactive_account.
                Account(
                    platform_id=listserv_platform.id,
                    key="listserv",
                    display_name="List",
                    credentials_ref="EMAIL_SECRET",
                    active=False,
                ),
            ]
        )
        await session.commit()

    batch = PostBatchRequest(
        source="n8n",
        posts=[
            _post("p-unknown-platform", targets=[("mastodon", "any")]),
            _post("p-unknown-account", targets=[("telegram", "ghost")]),
            _post("p-platform-disabled", targets=[("rss", "news_feed")]),
            _post("p-account-inactive", targets=[("email", "listserv")]),
        ],
    )

    result = await ingest_batch(session_factory, batch)
    codes = {r.external_id: [t.code for t in r.invalid_targets] for r in result.results}

    assert all(r.status is ItemStatus.REJECTED for r in result.results)
    assert codes["p-unknown-platform"] == ["unknown_platform"]
    assert codes["p-unknown-account"] == ["unknown_account"]
    assert codes["p-platform-disabled"] == ["platform_disabled"]
    assert codes["p-account-inactive"] == ["inactive_account"]
    assert await _counts(session_factory) == (0, 0)


async def test_repeated_target_in_one_item_creates_one_task(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    batch = PostBatchRequest(
        source="n8n",
        posts=[
            _post(
                "dup-targets",
                targets=[("telegram", "english_channel"), ("telegram", "english_channel")],
            )
        ],
    )

    result = await ingest_batch(session_factory, batch)

    assert result.results[0].status is ItemStatus.ACCEPTED
    assert result.results[0].created_tasks == 1
    assert await _counts(session_factory) == (1, 1)


async def test_metadata_and_created_at_are_immutable(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    key = "post-1"
    original = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    await ingest_batch(
        session_factory,
        PostBatchRequest(
            source="n8n", posts=[_post(key, metadata={"level": "A1"}, created_at=original)]
        ),
    )

    changed_metadata = await ingest_batch(
        session_factory,
        PostBatchRequest(
            source="n8n", posts=[_post(key, metadata={"level": "A2"}, created_at=original)]
        ),
    )
    assert changed_metadata.results[0].status is ItemStatus.CONFLICT
    assert "metadata" in changed_metadata.results[0].message

    changed_created = await ingest_batch(
        session_factory,
        PostBatchRequest(
            source="n8n",
            posts=[
                _post(
                    key,
                    metadata={"level": "A1"},
                    created_at=datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
                )
            ],
        ),
    )
    assert changed_created.results[0].status is ItemStatus.CONFLICT
    assert "created_at" in changed_created.results[0].message

    # The same instant expressed in another time zone is not a conflict.
    same_instant = datetime(2026, 10, 30, 23, 0, tzinfo=ZoneInfo("Asia/Yekaterinburg"))
    same = await ingest_batch(
        session_factory,
        PostBatchRequest(
            source="n8n", posts=[_post(key, metadata={"level": "A1"}, created_at=same_instant)]
        ),
    )
    assert same.results[0].status is ItemStatus.DUPLICATE


async def test_empty_metadata_equivalent_to_absent(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    await ingest_batch(
        session_factory, PostBatchRequest(source="n8n", posts=[_post("post-1", metadata={})])
    )
    again = await ingest_batch(
        session_factory, PostBatchRequest(source="n8n", posts=[_post("post-1")])
    )
    assert again.results[0].status is ItemStatus.DUPLICATE


async def test_accepted_data_persists_across_restart(tmp_path) -> None:
    """Spec 17: accepted data persists after the application restarts."""
    db_file = tmp_path / "publisher.db"
    url = f"sqlite+aiosqlite:///{db_file}"

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_session_factory(engine)
    await _seed_telegram_account(factory)
    accepted = await ingest_batch(factory, PostBatchRequest(source="n8n", posts=[_post("post-1")]))
    assert accepted.results[0].status is ItemStatus.ACCEPTED
    await engine.dispose()

    # "Restart": a brand-new engine over the same file.
    engine2 = create_async_engine(url)
    factory2 = make_session_factory(engine2)
    replay = await ingest_batch(factory2, PostBatchRequest(source="n8n", posts=[_post("post-1")]))
    assert replay.results[0].status is ItemStatus.DUPLICATE
    assert replay.results[0].post_id == accepted.results[0].post_id
    assert await _counts(factory2) == (1, 1)
    await engine2.dispose()


async def test_media_items_are_preserved_in_stored_content(session_factory) -> None:
    await _seed_telegram_account(session_factory)
    item = _post("with-media")
    item.content.media.append(MediaItem(type="photo", url="https://example.test/pic.jpg"))

    result = await ingest_batch(session_factory, PostBatchRequest(source="n8n", posts=[item]))

    assert result.results[0].status is ItemStatus.ACCEPTED
    async with session_factory() as session:
        post = await session.get(Post, result.results[0].post_id)
        assert post.content_json["media"] == [{"type": "photo", "url": "https://example.test/pic.jpg"}]


async def test_demo_data_behaves_as_accepted_data_for_idempotency(session_factory) -> None:
    """The demo dataset's posts are recognized as already-accepted data."""
    async with session_factory() as session:
        await populate_demo_data(session)

    result = await ingest_batch(
        session_factory,
        PostBatchRequest(
            source="n8n",
            posts=[
                _post("english-dialogue-000125", metadata={"language": "en", "level": "A1"})
            ],
        ),
    )

    assert result.results[0].status is ItemStatus.DUPLICATE
