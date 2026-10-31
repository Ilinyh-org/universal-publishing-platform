"""REST API tests (specification §7, §13, §17 "API and ingestion").

TestClient over ASGI with in-memory (or `tmp_path` file) SQLite — no live
server, no real platform calls, no real secrets (AGENTS.md §5). The API key
below is a dummy test value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine

from publisher.core.config import Settings
from publisher.db.models import Base, PublicationAttempt, PublicationTask
from publisher.db.session import create_engine as create_async_engine
from publisher.db.session import make_session_factory
from publisher.main import create_app

TEST_API_KEY = "test-key-0123456789abcdef"
AUTH = {"X-API-Key": TEST_API_KEY}
_IN_MEMORY = "sqlite+aiosqlite:///:memory:"


def _settings(database_url: str = _IN_MEMORY) -> Settings:
    return Settings(
        app_env="test",
        api_key=TEST_API_KEY,
        scheduler_enabled=False,
        database_url=database_url,
    )


def _make_engine(database_url: str) -> AsyncEngine:
    async def _create() -> AsyncEngine:
        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return engine

    return asyncio.run(_create())


async def _seed(engine: AsyncEngine) -> None:
    from publisher.db.models import Account, Platform

    factory = make_session_factory(engine)
    async with factory() as session:
        platform = Platform(key="telegram", display_name="Telegram")
        session.add(
            Account(
                platform=platform,
                key="english_channel",
                display_name="English A1",
                credentials_ref="TELEGRAM_BOT_TOKEN",
                configuration_json={"chat_id": "-1000000000000"},
            )
        )
        await session.commit()


@contextmanager
def api_client(
    database_url: str = _IN_MEMORY, *, seed: bool = True, **settings_overrides: Any
) -> Iterator[tuple[TestClient, AsyncEngine]]:
    """A TestClient over a fresh SQLite database, plus its engine."""
    settings = _settings(database_url)
    for key, value in settings_overrides.items():
        setattr(settings, key, value)
    engine = _make_engine(database_url)
    try:
        if seed:
            asyncio.run(_seed(engine))
        app = create_app(settings=settings, engine=engine)
        with TestClient(app) as client:
            yield client, engine
    finally:
        asyncio.run(engine.dispose())


def _post_payload(
    external_id: str,
    *,
    body: str = "Can I have a coffee?\nSure!",
    post_type: str = "dialogue",
    targets: list[dict[str, str]] | None = None,
    source: str = "n8n",
) -> dict[str, Any]:
    return {
        "source": source,
        "posts": [
            {
                "external_id": external_id,
                "content": {"type": "text", "title": "At the coffee shop", "body": body},
                "targets": targets
                or [{"platform": "telegram", "account": "english_channel"}],
                "post_type": post_type,
            }
        ],
    }


def _batch(source: str, count: int, *, prefix: str = "id") -> dict[str, Any]:
    return {
        "source": source,
        "posts": [
            {
                "external_id": f"{prefix}-{index:03d}",
                "content": {"type": "text", "body": f"body {index}"},
                "targets": [{"platform": "telegram", "account": "english_channel"}],
                "post_type": "dialogue",
            }
            for index in range(count)
        ],
    }


# ---------------------------------------------------------------- authentication


def test_health_is_unauthenticated() -> None:
    with api_client() as (client, _):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_api_key_required() -> None:
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=_post_payload("p-1"))
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert client.get("/api/v1/tasks").status_code == 401
        assert client.get("/api/v1/posts/some-id").status_code == 401


def test_invalid_api_key_rejected() -> None:
    with api_client() as (client, _):
        response = client.post(
            "/api/v1/posts", json=_post_payload("p-1"), headers={"X-API-Key": "wrong-key"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert client.get("/api/v1/tasks", headers={"X-API-Key": "wrong-key"}).status_code == 401


def test_wrong_scheme_rejected() -> None:
    """A `Bearer` token is not an API key: still 401, no info leak."""
    with api_client() as (client, _):
        response = client.get(
            "/api/v1/tasks", headers={"Authorization": f"Bearer {TEST_API_KEY}"}
        )
        assert response.status_code == 401


# ------------------------------------------------------------- batch boundaries


def test_batch_of_one_accepted() -> None:
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=_batch("n8n", 1), headers=AUTH)
        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 1
        assert results[0]["status"] == "accepted"
        assert results[0]["created_tasks"] == 1
        assert results[0]["post_id"]


def test_batch_of_ten_accepted() -> None:
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=_batch("n8n", 10), headers=AUTH)
        assert response.status_code == 200
        results = response.json()["results"]
        assert [r["status"] for r in results] == ["accepted"] * 10
        listing = client.get("/api/v1/tasks", headers=AUTH).json()
        assert listing["total"] == 10


def test_empty_batch_rejected() -> None:
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=_batch("n8n", 0), headers=AUTH)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_batch_of_eleven_rejected() -> None:
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=_batch("n8n", 11), headers=AUTH)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"
        # Rejected at the envelope: nothing persisted.
        assert client.get("/api/v1/tasks", headers=AUTH).json()["total"] == 0


# ---------------------------------------------------- per-item independent results


def test_partial_batch_success() -> None:
    """A rejected item never blocks a valid sibling (specification §6.1)."""
    payload = {
        "source": "n8n",
        "posts": [
            {
                "external_id": "good-1",
                "content": {"type": "text", "body": "valid"},
                "targets": [{"platform": "telegram", "account": "english_channel"}],
                "post_type": "dialogue",
            },
            {
                "external_id": "bad-1",
                "content": {"type": "text", "body": "unknown platform"},
                "targets": [{"platform": "twitter", "account": "english_channel"}],
                "post_type": "dialogue",
            },
        ],
    }
    with api_client() as (client, engine):
        response = client.post("/api/v1/posts", json=payload, headers=AUTH)
        assert response.status_code == 200
        by_id = {r["external_id"]: r for r in response.json()["results"]}
        assert by_id["good-1"]["status"] == "accepted"
        assert by_id["good-1"]["created_tasks"] == 1
        assert by_id["bad-1"]["status"] == "rejected"
        assert by_id["bad-1"]["invalid_targets"][0]["code"] == "unknown_platform"
        assert by_id["bad-1"]["post_id"] is None
        # Only the valid item's task exists.
        assert client.get("/api/v1/tasks", headers=AUTH).json()["total"] == 1
        del engine


def test_invalid_target_mix_within_one_item() -> None:
    """Valid targets create tasks; invalid ones are reported (specification §6.4)."""
    payload = _post_payload("mixed-1")
    payload["posts"][0]["targets"] = [
        {"platform": "telegram", "account": "english_channel"},
        {"platform": "telegram", "account": "ghost"},
    ]
    with api_client() as (client, _):
        response = client.post("/api/v1/posts", json=payload, headers=AUTH)
        result = response.json()["results"][0]
        assert result["status"] == "accepted"
        assert result["created_tasks"] == 1
        assert [t["code"] for t in result["invalid_targets"]] == ["unknown_account"]


def test_inactive_account_rejected() -> None:
    with api_client() as (client, engine):
        asyncio.run(_set_account_active(engine, False))
        response = client.post("/api/v1/posts", json=_post_payload("p-9"), headers=AUTH)
        result = response.json()["results"][0]
        assert result["status"] == "rejected"
        assert result["invalid_targets"][0]["code"] == "inactive_account"


async def _set_account_active(engine: AsyncEngine, active: bool) -> None:
    from sqlalchemy import update

    from publisher.db.models import Account

    factory = make_session_factory(engine)
    async with factory() as session:
        await session.execute(update(Account).values(active=active))
        await session.commit()


# --------------------------------------------------------------- idempotency


def test_duplicate_submission_is_idempotent() -> None:
    batch = _batch("n8n", 2)
    with api_client() as (client, _):
        first = client.post("/api/v1/posts", json=batch, headers=AUTH).json()
        second = client.post("/api/v1/posts", json=batch, headers=AUTH).json()
        assert [r["status"] for r in first["results"]] == ["accepted", "accepted"]
        assert [r["status"] for r in second["results"]] == ["duplicate", "duplicate"]
        assert all(r["created_tasks"] == 0 for r in second["results"])
        assert [r["post_id"] for r in second["results"]] == [r["post_id"] for r in first["results"]]
        # No duplicate tasks for the same post/account pair (specification §6.5).
        assert client.get("/api/v1/tasks", headers=AUTH).json()["total"] == 2


def test_conflicting_duplicate_rejected_without_overwrite() -> None:
    with api_client() as (client, _):
        client.post("/api/v1/posts", json=_post_payload("c-1"), headers=AUTH)
        conflict = client.post(
            "/api/v1/posts",
            json=_post_payload("c-1", body="A different body"),
            headers=AUTH,
        ).json()["results"][0]
        assert conflict["status"] == "conflict"
        # The original content is intact.
        tasks = client.get("/api/v1/tasks", headers=AUTH).json()["items"]
        post = client.get(f"/api/v1/posts/{tasks[0]['post_id']}", headers=AUTH).json()
        assert post["content"]["body"] == "Can I have a coffee?\nSure!"


def test_same_external_id_different_source_is_distinct() -> None:
    with api_client() as (client, _):
        first = client.post(
            "/api/v1/posts", json=_post_payload("shared", source="n8n"), headers=AUTH
        )
        second = client.post(
            "/api/v1/posts", json=_post_payload("shared", source="other"), headers=AUTH
        )
        assert first.json()["results"][0]["status"] == "accepted"
        assert second.json()["results"][0]["status"] == "accepted"


# ----------------------------------------------------------------- persistence


def test_persistence_across_restart(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'publisher.db'}"
    with api_client(database_url) as (client, _):
        results = client.post("/api/v1/posts", json=_batch("n8n", 2), headers=AUTH).json()[
            "results"
        ]
        post_ids = {r["post_id"] for r in results}

    # "Restart": a brand-new engine and app instance over the same file.
    # No re-seed: the seeded platform/account rows persisted with the file.
    with api_client(database_url, seed=False) as (client, _):
        for post_id in post_ids:
            response = client.get(f"/api/v1/posts/{post_id}", headers=AUTH)
            assert response.status_code == 200
        assert client.get("/api/v1/tasks", headers=AUTH).json()["total"] == 2
        duplicate = client.post("/api/v1/posts", json=_batch("n8n", 2), headers=AUTH).json()
        assert [r["status"] for r in duplicate["results"]] == ["duplicate", "duplicate"]


# ------------------------------------------------------------------- read APIs


def test_get_post_by_id() -> None:
    with api_client() as (client, _):
        created = client.post("/api/v1/posts", json=_post_payload("g-1"), headers=AUTH).json()
        post_id = created["results"][0]["post_id"]
        response = client.get(f"/api/v1/posts/{post_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == post_id
        assert body["source"] == "n8n"
        assert body["external_id"] == "g-1"
        assert body["content"]["body"] == "Can I have a coffee?\nSure!"
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["status"] == "pending"
        assert body["tasks"][0]["account"]["platform"] == "telegram"
        assert body["tasks"][0]["account"]["key"] == "english_channel"


def test_get_unknown_post_404() -> None:
    with api_client() as (client, _):
        response = client.get("/api/v1/posts/00000000-0000-0000-0000-000000000000", headers=AUTH)
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "post not found",
                                             "details": None}}


def test_task_list_filters_and_pagination() -> None:
    with api_client() as (client, _):
        client.post(
            "/api/v1/posts", json=_post_payload("f-1", post_type="dialogue"), headers=AUTH
        )
        client.post(
            "/api/v1/posts", json=_post_payload("f-2", post_type="grammar"), headers=AUTH
        )
        client.post(
            "/api/v1/posts", json=_post_payload("f-3", post_type="grammar"), headers=AUTH
        )

        listing = client.get("/api/v1/tasks", headers=AUTH).json()
        assert listing["total"] == 3
        assert listing["limit"] == 20 and listing["offset"] == 0

        assert client.get("/api/v1/tasks", params={"post_type": "grammar"}, headers=AUTH).json()[
            "total"
        ] == 2
        assert client.get("/api/v1/tasks", params={"post_type": "vocabulary"}, headers=AUTH).json()[
            "total"
        ] == 0
        assert client.get("/api/v1/tasks", params={"status": "pending"}, headers=AUTH).json()[
            "total"
        ] == 3
        assert client.get("/api/v1/tasks", params={"status": "published"}, headers=AUTH).json()[
            "total"
        ] == 0
        assert client.get("/api/v1/tasks", params={"platform": "telegram"}, headers=AUTH).json()[
            "total"
        ] == 3
        assert client.get("/api/v1/tasks", params={"platform": "twitter"}, headers=AUTH).json()[
            "total"
        ] == 0
        assert client.get(
            "/api/v1/tasks", params={"account": "english_channel"}, headers=AUTH
        ).json()[
            "total"
        ] == 3
        assert client.get("/api/v1/tasks", params={"account": "other"}, headers=AUTH).json()[
            "total"
        ] == 0

        # Bounded date range around "now".
        now = datetime.now(UTC)
        inside = client.get(
            "/api/v1/tasks",
            params={
                "created_from": (now - timedelta(hours=1)).isoformat(),
                "created_to": (now + timedelta(hours=1)).isoformat(),
            },
            headers=AUTH,
        ).json()
        assert inside["total"] == 3
        before = client.get(
            "/api/v1/tasks", params={"created_to": (now - timedelta(hours=1)).isoformat()},
            headers=AUTH,
        ).json()
        assert before["total"] == 0

        # Pagination edges.
        page = client.get("/api/v1/tasks", params={"limit": 2, "offset": 0}, headers=AUTH).json()
        assert len(page["items"]) == 2 and page["total"] == 3
        last = client.get("/api/v1/tasks", params={"limit": 2, "offset": 2}, headers=AUTH).json()
        assert len(last["items"]) == 1
        beyond = client.get("/api/v1/tasks", params={"offset": 10}, headers=AUTH).json()
        assert beyond["items"] == [] and beyond["total"] == 3


def test_task_list_filter_validation() -> None:
    with api_client() as (client, _):
        assert client.get("/api/v1/tasks", params={"limit": 0}, headers=AUTH).status_code == 422
        assert client.get("/api/v1/tasks", params={"limit": 101}, headers=AUTH).status_code == 422
        assert client.get("/api/v1/tasks", params={"offset": -1}, headers=AUTH).status_code == 422
        assert client.get(
            "/api/v1/tasks", params={"status": "bogus"}, headers=AUTH
        ).status_code == 422


def test_get_task_detail_with_attempts(tmp_path: Path) -> None:
    database_url = _IN_MEMORY
    with api_client(database_url) as (client, engine):
        created = client.post("/api/v1/posts", json=_post_payload("t-1"), headers=AUTH).json()
        tasks = client.get("/api/v1/tasks", headers=AUTH).json()["items"]
        task_id = tasks[0]["id"]
        asyncio.run(_add_attempt(engine, task_id))

        response = client.get(f"/api/v1/tasks/{task_id}", headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == task_id
        assert body["status"] == "pending"
        assert body["post"]["external_id"] == "t-1"
        assert body["post"]["post_type"] == "dialogue"
        assert [a["attempt_number"] for a in body["attempts"]] == [1]
        assert body["attempts"][0]["success"] is False
        assert body["attempts"][0]["error_code"] == "temporary_error"

        unknown = client.get("/api/v1/tasks/nope", headers=AUTH)
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"
        del created


async def _add_attempt(engine: AsyncEngine, task_id: str) -> None:
    factory = make_session_factory(engine)
    async with factory() as session:
        task = await session.get(PublicationTask, task_id)
        assert task is not None
        session.add(
            PublicationAttempt(
                task_id=task_id,
                attempt_number=1,
                finished_at=datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
                success=False,
                error_code="temporary_error",
                error_message="sanitized demo failure",
                response_metadata_json={"ok": False},
            )
        )
        await session.commit()


# --------------------------------------------------------------- consistency


def test_no_secrets_in_any_response() -> None:
    with api_client() as (client, _):
        created = client.post("/api/v1/posts", json=_post_payload("s-1"), headers=AUTH)
        post_id = created.json()["results"][0]["post_id"]
        task_id = client.get("/api/v1/tasks", headers=AUTH).json()["items"][0]["id"]
        bodies = [
            client.get("/health").text,
            created.text,
            client.get(f"/api/v1/posts/{post_id}", headers=AUTH).text,
            client.get("/api/v1/tasks", headers=AUTH).text,
            client.get(f"/api/v1/tasks/{task_id}", headers=AUTH).text,
            client.post("/api/v1/posts", json=_post_payload("s-1"), headers=AUTH).text,
        ]
        for body in bodies:
            assert TEST_API_KEY not in body
            assert "credentials_ref" not in body
            assert "configuration_json" not in body
            assert "chat_id" not in body
            assert "TELEGRAM_BOT_TOKEN" not in body


def test_unknown_route_returns_error_object() -> None:
    with api_client() as (client, _):
        response = client.get("/api/v1/unknown", headers=AUTH)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_malformed_json_returns_error_object() -> None:
    with api_client() as (client, _):
        response = client.post(
            "/api/v1/posts",
            content="{not json",
            headers={**AUTH, "Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_openapi_document_is_served() -> None:
    with api_client() as (client, _):
        schema = client.get("/openapi.json").json()
        assert set(schema["paths"]) == {
            "/health",
            "/api/v1/posts",
            "/api/v1/posts/{post_id}",
            "/api/v1/tasks",
            "/api/v1/tasks/{task_id}",
        }
        # The itemized batch response schema is documented (specification §6.6).
        post_schema = schema["components"]["schemas"]["BatchIngestionResult"]
        assert post_schema["properties"]["results"]["items"]["$ref"]
        assert client.get("/docs").status_code == 200
