# Universal Publishing Platform

A self-hosted publishing service that receives universal posts from systems such as n8n, stores them, schedules publication, and delivers them through platform adapters.

The first adapter is Telegram. The application is designed to run from the same Docker image in standalone Docker and TrueNAS SCALE Custom Apps.

## Documentation

- [Technical specification](docs/TECHNICAL_SPECIFICATION.md) — authoritative requirements.
- [Architecture](docs/ARCHITECTURE.md) — components, data flow, database relationships, and adapter boundary.
- [AI agent instructions](AGENTS.md) — mandatory rules for coding agents.

## Initial technology

Python, FastAPI, Pydantic, SQLAlchemy, Alembic, SQLite, APScheduler, and httpx.

## Configuration

Copy `.env.example` to `.env` and replace all placeholder secrets. Never commit `.env` or real credentials.

The default API port is `8000`. The default persistent data directory is `/app/data`. Mount this directory to persistent storage so the SQLite database survives container recreation and updates.

## Migrations

The schema is managed with Alembic (async-capable; the migration target mirrors the SQLAlchemy models in `src/publisher/db/models.py`):

```bash
alembic upgrade head
```

The database URL is read from the `DATABASE_URL` environment variable and falls back to the `Settings` default (`sqlite+aiosqlite:////app/data/publisher.db`). Override it per run, e.g. for a scratch database:

```bash
DATABASE_URL=sqlite:////tmp/publisher.db alembic upgrade head
alembic downgrade base   # drops all application tables
```

Run migrations as a documented deployment step before starting the service; avoid running competing migrations from multiple app replicas (specification §15).

## Ingestion service

`src/publisher/services/ingestion.py` implements the ingestion contract of specification §6: each submitted item is processed in its own transaction, `(source, external_id)` is the idempotency key (identical resubmissions are no-op duplicates, differing immutable data is a conflict), targets resolve against the `platforms`/`accounts` tables (one `pending` publication task per valid target, per-item error codes for invalid ones), and results are itemized per submitted post. The REST endpoint `POST /api/v1/posts` exposes it directly.

## Scheduling and platform adapters

`src/publisher/core/scheduling.py` implements schedule assignment (specification §9): it matches `pending` tasks to active `publishing_schedules` rows by account and post type, computes the next eligible slot from ISO weekdays (1=Monday … 7=Sunday) in the schedule's IANA time zone, and stores the UTC instant in `scheduled_at` with status `scheduled`. Tasks that already carry a `scheduled_at` are never moved by routine runs; tasks without a matching schedule stay `pending` and are visible through the API. Documented DST policy (PLAN step-4 proposal, awaiting owner sign-off): ambiguous local times resolve to the earlier offset, nonexistent local times to the next valid instant (the end of the gap).

Slot exclusivity (spec §9.2) is enforced twice: a database-level unique index on `(account_id, scheduled_at)` (migration `c4d8e21a95f0`) and occupied-slot skipping inside the single assignment transaction.

The first platform adapter lives in `src/publisher/adapters/telegram/`: `sendMessage` in HTML parse mode, chat id from the account configuration, bot token resolved through the account's secret reference (never logged — an httpx log filter redacts token-shaped URL segments), Telegram's `message_id` recorded as `external_post_id`, classified errors including `retry_after` handling, and message-length validation against the 4096-character Bot API limit.

The `SchedulerService` (APScheduler interval job) runs the assignment pass periodically; together with the publishing engine it is started by the application lifespan when `ENGINE_ENABLED=true` (see below).

## Publishing engine and reliability

`src/publisher/core/publishing.py` implements due-task processing (specification §9.3, §10). One engine pass:

1. **Stale-claim recovery** — tasks stuck in `publishing` longer than `STALE_PUBLISHING_TIMEOUT_SECONDS` are returned to the queue: the unfinished attempt is closed with the `stale_claim` error code and a message recording the delivery ambiguity (the platform may have accepted the request, so a duplicate external post is possible; exactly-once is not promised), and the task goes to `retry_wait` — or to `failed` when `MAX_ATTEMPTS` is exhausted.
2. **Missed-slot downtime policy (§9.2)** — the engine keeps a heartbeat (`engine_state`, one row). When the gap since the previous tick exceeds `STALE_PUBLISHING_TIMEOUT_SECONDS` (or on the very first tick after enabling the engine), due `scheduled` tasks are treated as missed: their slot is skipped, the task moves to its next future schedule slot, and the rescheduling is logged. Unscheduled `pending` tasks always receive future slots through the scheduler's assignment pass. Due tasks with no usable active schedule stay due and publish normally (logged).
3. **Due-task publishing** — `scheduled` tasks with `scheduled_at <= now` and `retry_wait` tasks with `next_attempt_at <= now` are claimed atomically (a conditional `UPDATE … WHERE status = …`, so no two workers can hold one task) with the attempt record created in the same transaction. The adapter call happens outside any database transaction; success (`published`, `published_at`, `external_post_id`, sanitized response metadata), retry (`retry_wait` + `next_attempt_at`), and permanent failure (`failed`) are each finalized in one transaction. One task failing never blocks another.

Retries use exponential backoff — `RETRY_BASE_DELAY_SECONDS × 2^(attempt−1)` capped at `RETRY_MAX_DELAY_SECONDS`, with jitter in the upper half — and honor platform-provided retry delays (Telegram `retry_after`) exactly. After `MAX_ATTEMPTS` total attempts (default 5) a task is `failed`. Adapters are resolved through the registry (`adapters/registry.py`); credentials are resolved per account through the secret provider (`core/secrets.py`) from the account's `credentials_ref`, never stored in the database or logs.

## Running the API

Set at least the API key, apply migrations, and start uvicorn:

```bash
export API_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export DATABASE_URL=sqlite+aiosqlite:////app/data/publisher.db   # or your path
alembic upgrade head
uvicorn publisher.main:app --host 0.0.0.0 --port 8000
```

In production (`APP_ENV=production`) startup refuses to serve unless `API_KEY` is set (and the Telegram bot token when the scheduler or engine is enabled) — `Settings.validate_production()` runs in the application lifespan. `APP_HOST`/`APP_PORT` mirror the uvicorn bind options.

Background work (schedule assignment plus the publishing engine) starts automatically with the API when `ENGINE_ENABLED=true` (the default) and stops gracefully on shutdown (SIGTERM/SIGINT drain an in-flight tick). Set `ENGINE_ENABLED=false` for deterministic tests, CLI-only use, or API-only replicas; `SCHEDULER_ENABLED=false` disables only the schedule-assignment pass.

Interactive documentation: `/docs` and `/openapi.json`.

Submit a batch (1–10 posts; every item gets its own result):

```bash
curl -X POST http://localhost:8000/api/v1/posts \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{
    "source": "n8n",
    "posts": [{
      "external_id": "english-dialogue-000125",
      "content": {"type": "text", "title": "At the coffee shop", "body": "Can I have a coffee?\nSure!"},
      "targets": [{"platform": "telegram", "account": "english_channel"}],
      "post_type": "dialogue"
    }]
  }'
```

Read endpoints take the same header: `GET /api/v1/posts/{post_id}`, `GET /api/v1/tasks?status=pending&limit=20&offset=0`, `GET /api/v1/tasks/{task_id}`. Everything except `GET /health` requires `X-API-Key`; every error is `{"error": {"code", "message", "details"}}`.

## API overview

- `GET /health` — health check, no API key required.
- `POST /api/v1/posts` — submit a batch of 1–10 posts; requires `X-API-Key`.
- `GET /api/v1/posts/{post_id}` — retrieve post and target statuses.
- `GET /api/v1/tasks` — list/filter publication tasks.
- `GET /api/v1/tasks/{task_id}` — retrieve a task and attempt history.

See the technical specification and generated OpenAPI documentation for the full contract.

## Development status

Delivered so far (roadmap in `docs/PLAN.md`):

- **Step 1 — scaffold:** package layout, settings, universal content models, adapter contract, SQLAlchemy models, session factory, read-only task listing, `publisher` CLI.
- **Step 2 — migrations & ingestion:** Alembic setup with the initial migration creating all six tables (`alembic upgrade head`), and the ingestion service with idempotency, conflict detection, and target resolution.
- **Step 3 — REST API:** FastAPI app with unauthenticated `/health`, `X-API-Key` authentication (constant-time compare), `POST /api/v1/posts` (itemized batch ingestion), `GET /api/v1/posts/{post_id}`, `GET /api/v1/tasks` (filters + pagination), `GET /api/v1/tasks/{task_id}`, consistent error objects, and OpenAPI documentation.
- **Step 4 — scheduler & Telegram adapter:** schedule assignment (`core/scheduling.py`) with IANA time zones, ISO weekdays, a documented DST policy, and database-enforced slot exclusivity; the Telegram adapter (`adapters/telegram/`) with HTML `sendMessage`, secret-reference bot tokens, `message_id` → `external_post_id`, `retry_after` error classification, and length validation.
- **Step 5 — publishing engine & reliability:** the engine (`core/publishing.py`) with atomic task claiming + attempt records, due `scheduled`/`retry_wait` processing, exponential backoff with jitter capped by `MAX_ATTEMPTS` (default 5), exact rate-limit delays, stale-`publishing` recovery, and the missed-slot downtime policy with logged rescheduling; the adapter registry, the secret provider, and lifespan wiring of scheduler + engine behind `ENGINE_ENABLED`.

Next: deployment (step 6) — Dockerfile, Compose example, healthcheck, and TrueNAS SCALE Custom App readiness.

This repository documentation defines the implementation baseline. Implement the system incrementally and keep tests and documentation aligned with the specification.
