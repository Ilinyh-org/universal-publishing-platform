"""Fixed demo dataset for the CLI `--demo` mode and tests.

Obviously fake content everywhere; no real channel names, chat ids, or
credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Account,
    Platform,
    Post,
    PublicationTask,
    PublishingSchedule,
    TaskStatus,
)


async def populate_demo_data(session: AsyncSession) -> None:
    """Insert a small fixed dataset covering three task states."""
    telegram = Platform(key="telegram", display_name="Telegram")
    account = Account(
        platform=telegram,
        key="english_channel",
        display_name="English A1 (demo)",
        credentials_ref="TELEGRAM_BOT_TOKEN",
        configuration_json={"chat_id": "set-at-deployment"},
    )
    session.add_all(
        [
            PublishingSchedule(account=account, post_type="dialogue", publish_time=time(9, 0)),
            PublishingSchedule(account=account, post_type="grammar", publish_time=time(18, 30)),
        ]
    )
    dialogue = Post(
        source="n8n",
        external_id="english-dialogue-000125",
        content_json={
            "type": "text",
            "title": "At the coffee shop",
            "body": "Can I have a coffee?\nSure!",
            "format": "plain_text",
            "media": [],
        },
        post_type="dialogue",
        metadata_json={"language": "en", "level": "A1"},
    )
    grammar = Post(
        source="n8n",
        external_id="english-grammar-000042",
        content_json={
            "type": "text",
            "title": "Present Simple: do/does",
            "body": "I do. You do. He does.",
            "format": "plain_text",
            "media": [],
        },
        post_type="grammar",
    )
    vocabulary = Post(
        source="n8n",
        external_id="english-vocabulary-000007",
        content_json={
            "type": "text",
            "title": "Weather words",
            "body": "sunny, rainy, windy, cloudy",
            "format": "plain_text",
            "media": [],
        },
        post_type="vocabulary",
    )
    session.add_all(
        [
            PublicationTask(post=dialogue, account=account, status=TaskStatus.PENDING),
            PublicationTask(
                post=grammar,
                account=account,
                status=TaskStatus.SCHEDULED,
                scheduled_at=datetime(2026, 11, 2, 13, 0, tzinfo=UTC),
            ),
            PublicationTask(
                post=vocabulary,
                account=account,
                status=TaskStatus.RETRY_WAIT,
                next_attempt_at=datetime(2026, 10, 30, 21, 0, tzinfo=UTC),
                attempt_count=1,
                last_error_code="temporary",
                last_error_message="demo: Telegram timeout",
            ),
        ]
    )
    await session.commit()
