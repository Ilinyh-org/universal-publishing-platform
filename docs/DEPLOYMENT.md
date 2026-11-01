# Deployment Guide

Operational deployment for the Universal Publishing Platform: one portable
image, environment-driven configuration, standalone Docker and TrueNAS SCALE
Custom App readiness (specification §15).

## 1. The image

`Dockerfile` (multi-stage, `python:3.12-slim`) builds one portable image for
all supported environments:

- **Non-root runtime.** A dedicated system user `upp` (uid/gid `10001`) runs
  the process; nothing in the container needs root.
- **Configurable port, default 8000.** The bind port follows the `APP_PORT`
  environment variable (specification §14). The `EXPOSE` statement documents
  the default at build time; overriding `APP_PORT` at runtime changes the
  actual bind (`docker run -e APP_PORT=9000 …`, Compose `environment:`), and
  the port mapping in Compose (`"host:container"`) is yours to set.
- **`/app/data` volume.** The persistent data directory (SQLite database,
  `DATA_DIR`, `DATABASE_URL` default `sqlite+aiosqlite:////app/data/publisher.db`)
  is declared with `VOLUME ["/app/data"]` and must be a mounted volume in
  production so data survives container recreation and updates.
- **Healthcheck on `/health`.** The image `HEALTHCHECK` polls
  `http://127.0.0.1:$APP_PORT/health` (unauthenticated, discloses no
  configuration — specification §16). `start-period` covers migration-started
  cold starts.
- **Graceful SIGTERM/SIGINT handling.** The exec-form entrypoint
  (`docker/entrypoint.sh`) makes uvicorn PID 1, so runtime signals reach it
  directly: uvicorn stops accepting new requests and the FastAPI lifespan
  shutdown stops the scheduler and publishing engine, draining the in-flight
  engine tick and disposing the database engine (step-5 wiring). Allow a stop
  timeout larger than the graceful-shutdown bound (default 30 s,
  `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN`): Compose `stop_grace_period: 60s` in
  the example, or `docker stop -t 60`.

No secrets are copied into or built into the image (`.dockerignore` excludes
`.env`, local databases, and caches); everything operational arrives through
environment variables (specification §14).

## 2. Configuration

Copy `.env.example` to `.env` and replace the placeholders. The Compose
example loads it with `env_file:`; standalone `docker run` passes the same
variables with `--env-file .env`.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `production` | `production` enables strict startup validation. |
| `APP_HOST` | `0.0.0.0` | Bind address inside the container. |
| `APP_PORT` | `8000` | Application port (specification §14). |
| `LOG_LEVEL` | `INFO` | Application log level. |
| `DATA_DIR` | `/app/data` | Persistent data directory (volume mount point). |
| `DATABASE_URL` | `sqlite+aiosqlite:////app/data/publisher.db` | Database URL; keep it under `/app/data` in containers. |
| `API_KEY` | — (required in production) | `X-API-Key` value for every endpoint except `/health`. **Secret.** |
| `ENGINE_ENABLED` | `true` | Master switch for scheduler + publishing engine in this process. Set `false` on API-only replicas. |
| `SCHEDULER_ENABLED` | `true` | Schedule-assignment pass only. |
| `SCHEDULER_POLL_INTERVAL_SECONDS` | `15` | Background poll interval. |
| `MAX_ATTEMPTS` | `5` | Retry cap per publication task. |
| `RETRY_BASE_DELAY_SECONDS` | `30` | Exponential backoff base. |
| `RETRY_MAX_DELAY_SECONDS` | `3600` | Backoff cap. |
| `STALE_PUBLISHING_TIMEOUT_SECONDS` | `900` | Stale-claim recovery threshold; also drives the missed-slot downtime policy. |
| `TELEGRAM_BOT_TOKEN` | — (required when scheduler/engine is on) | Bot token resolved through an account's `credentials_ref`. **Secret.** |
| `UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN` | `30` | Container-only: graceful-shutdown bound passed to uvicorn. |

### Secrets (never in the image, never in git)

- `API_KEY` and `TELEGRAM_BOT_TOKEN` are supplied **through the environment
  only** — via the `.env` file (git-ignored), `docker run --env-file`, or the
  secret/environment storage of the deployment platform.
- Adapter credentials resolve per account through the environment secret
  provider (`core/secrets.py`): an account row's `credentials_ref` names the
  environment variable — with one declared credential field (the Telegram
  adapter), `credentials_ref=TELEGRAM_BOT_TOKEN` reads that variable
  directly. Mounted-file secret providers can be added later without
  changing the configuration model.
- `.env.example` carries placeholders only; `.env`, `*.db`, and caches never
  enter the image build (`.dockerignore`).

## 3. Database migrations

Migrations are a **documented deployment step**, not an application-startup
step (specification §15). Run them once per release, before (re)starting the
service, from the same image so the code and schema match:

```bash
docker compose run --rm app alembic upgrade head
```

- The one-off run uses the same `DATABASE_URL` and volume as the service —
  pass the same `.env` (`docker compose run` applies `env_file:` of the
  service automatically; for plain `docker run` add `--env-file .env` and
  the volume mount).
- Run migrations from **one** place: never let multiple app replicas run
  competing migrations (specification §15). Scale to more replicas only
  after the single migration run has finished. (Version 1 supports a single
  process anyway — SQLite.)
- Verify before serving: `docker compose run --rm app alembic current`
  should print the latest revision.

## 4. Standalone Docker (Compose example)

```bash
# 1. Network: create/name it yourself; never depend on a platform-created name.
docker network create platform-net

# 2. Configuration: copy the example and fill in the secrets.
cp .env.example .env && $EDITOR .env

# 3. Build.
docker compose build

# 4. Migrate (once per release; see §3).
docker compose run --rm app alembic upgrade head

# 5. Start and verify.
docker compose up -d
docker compose ps                     # health: healthy
curl -s http://localhost:8000/health  # unauthenticated status
```

The example `docker-compose.yml` demonstrates every §15 requirement:
`restart: unless-stopped`, the named `app-data` volume at `/app/data`,
`env_file: .env`, attachment to an existing external network
(`networks.platform.external: true` — point `name:` at your own network), a
60 s stop-grace period for the drain, and the image-borne `/health`
healthcheck.

Standalone `docker run` equivalent (same image, same configuration model):

```bash
docker build -t universal-publishing-platform:latest .
docker run -d --name upp \
  --env-file .env \
  -p 8000:8000 \
  --network platform-net \
  --mount type=volume,src=upp-data,dst=/app/data \
  --restart unless-stopped \
  --stop-timeout 60 \
  universal-publishing-platform:latest
```

## 5. TrueNAS SCALE Custom App readiness

The same image and configuration model work unchanged as a TrueNAS SCALE
Custom App ("Custom App" = your own Compose/`docker run` definition via the
GUI's YAML editor):

1. **Image:** build it (`docker compose build`) and push to a registry the
   host can pull from, or build directly on the TrueNAS host. Reference the
   same image tag in the Custom App YAML — no TrueNAS-specific image exists.
2. **Network:** create the network yourself (or reuse an existing
   user-created one) and reference it as `external: true`. The deployment
   must **not** depend on a network name created by TrueNAS or any other
   platform (specification §15).
3. **Storage:** mount a dataset/host path to `/app/data` (the image's
   declared volume). Keep `DATABASE_URL` pointing at that directory.
4. **Environment:** supply all variables from §2 through the Custom App's
   environment/secret configuration — identical names and semantics as
   standalone Docker. `APP_HOST=0.0.0.0` (container-internal bind) and the
   published-port mapping stay your choice; the port itself follows
   `APP_PORT`.
5. **Migrations:** before starting the app (and after every image update),
   run the one-off migration command from the same image with the same
   volume and environment — e.g. a temporary job/`docker run` executing
   `alembic upgrade head` — then start the app. One migration runner at a
   time.
6. **Health:** the image-borne `/health` healthcheck applies as-is; wire the
   platform's liveness probe to it if the Custom App template exposes one.
7. **Shutdown:** keep a stop timeout above the graceful-shutdown bound so
   in-flight publications drain (§1).

## 6. Operational notes

- **Replicas:** version 1 supports a single app process (SQLite,
  specification §5). Do not scale beyond one instance without owner approval
  (AGENTS.md §2). `ENGINE_ENABLED=false` makes an instance API-only if you
  ever need to split ingestion from publishing.
- **Logs:** the container logs to stdout (`PYTHONUNBUFFERED=1`); collect via
  the platform's driver. Logs never contain API keys, tokens, or
  authorization material (specification §16).
- **Backups:** back up the volume (the SQLite file) on a schedule; stop the
  container (or quiesce writes) for a consistent file copy, and verify the
  copy restores.

## 7. Rollback

1. **Stop the service** (`docker compose down` — the volume survives).
2. **Back up the volume** before any schema-touching step.
3. **Redeploy the previous image tag** (`image:`/`build` reference) with the
   same configuration. Because migrations are an explicit step, an older app
   image runs fine on a schema it predates; do not run `alembic downgrade`
   unless the rollback target's code requires the older schema — and run it
   from the *rollback image*, once, against a backup-taken volume.
4. **Verify:** `/health` returns 200; `docker compose run --rm app alembic
   current` shows the expected revision; submit a test batch through
   `POST /api/v1/posts`.

## 8. Acceptance checklist (PLAN step 6 / specification §15)

- [x] One portable image for all supported environments (multi-stage build).
- [x] Non-root user (`upp`, uid/gid 10001).
- [x] Configurable application port, default 8000 (`APP_PORT`).
- [x] `/app/data` default data directory, `VOLUME`-declared.
- [x] SQLite database persisted in a mounted volume.
- [x] Docker healthcheck using `/health`.
- [x] `restart: unless-stopped` in the Compose example.
- [x] SIGTERM/SIGINT handled gracefully (exec entrypoint → uvicorn → lifespan
      drain of the in-flight engine tick; stop-grace period documented).
- [x] `docker-compose.yml` example with documented external-network
      attachment; no TrueNAS- or platform-created network names.
- [x] Same image + configuration model for standalone Docker and TrueNAS
      SCALE Custom Apps (§5).
- [x] Migration execution documented as a deployment step; single migration
      runner; no competing replicas.
- [ ] Runtime verification (`docker build` + `/health` + SIGTERM stop test):
      **owner-deploy-time** — no Docker daemon exists in the development
      sandbox; the deployment files were validated statically (compose
      structure, Dockerfile review, structural tests in
      `tests/test_deployment.py`).
