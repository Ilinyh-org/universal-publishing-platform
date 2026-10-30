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

## API overview

- `GET /health` — health check, no API key required.
- `POST /api/v1/posts` — submit a batch of 1–10 posts; requires `X-API-Key`.
- `GET /api/v1/posts/{post_id}` — retrieve post and target statuses.
- `GET /api/v1/tasks` — list/filter publication tasks.
- `GET /api/v1/tasks/{task_id}` — retrieve a task and attempt history.

See the technical specification and generated OpenAPI documentation for the full contract.

## Development status

This repository documentation defines the implementation baseline. Implement the system incrementally and keep tests and documentation aligned with the specification.
