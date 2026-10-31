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

## Running the API

Set at least the API key, apply migrations, and start uvicorn:

```bash
export API_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')
export DATABASE_URL=sqlite+aiosqlite:////app/data/publisher.db   # or your path
alembic upgrade head
uvicorn publisher.main:app --host 0.0.0.0 --port 8000
```

In production (`APP_ENV=production`) startup refuses to serve unless `API_KEY` is set (and the Telegram bot token when the scheduler is enabled) — `Settings.validate_production()` runs in the application lifespan. `APP_HOST`/`APP_PORT` mirror the uvicorn bind options.

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

Next: the scheduler and Telegram adapter (step 4) — schedule assignment from `publishing_schedules` and the first platform adapter.

This repository documentation defines the implementation baseline. Implement the system incrementally and keep tests and documentation aligned with the specification.
