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

`src/publisher/services/ingestion.py` implements the ingestion contract of specification §6: each submitted item is processed in its own transaction, `(source, external_id)` is the idempotency key (identical resubmissions are no-op duplicates, differing immutable data is a conflict), targets resolve against the `platforms`/`accounts` tables (one `pending` publication task per valid target, per-item error codes for invalid ones), and results are itemized per submitted post. The REST endpoint that exposes it arrives with the API layer.

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

Next: the REST API (step 3) — FastAPI application, `X-API-Key` authentication, `POST /api/v1/posts`, and the query endpoints.

This repository documentation defines the implementation baseline. Implement the system incrementally and keep tests and documentation aligned with the specification.
