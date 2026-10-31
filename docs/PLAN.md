# Delivery Plan — Universal Publishing Platform

**Status:** Steps 1–4 of the step-by-step build delivered. Step 1 (scaffold)
was merged as pull request #1, step 2 (persistence migrations & ingestion
service) as pull request #2, step 3 (REST API) as the step-3 pull request,
and step 4 (scheduler & Telegram adapter) as the step-4 pull request.
**Authority:** `docs/TECHNICAL_SPECIFICATION.md` and `docs/ARCHITECTURE.md` are the
mandatory baseline (`AGENTS.md` §1). This document only sequences delivery into
reviewable steps; it does not change the architecture. Any deviation from the
specification requires explicit owner approval (`AGENTS.md` §2).

## 1. How the platform relates to the legacy n8n setup

Today (n8n workflow + `language_learning` PostgreSQL database):

1. The workflow generates posts (dialogue/grammar confirmed; tenses/vocabulary
   provisional reference data) and writes rows with status `pending` into the
   legacy `posts` table.
2. `publishing_schedule` slots (`Asia/Yekaterinburg`, per weekday) decide when
   the n8n Telegram node publishes.
3. Retries/status live in the legacy `posts` table
   (`pending → publishing → published/failed`, `attempts`, `last_error`).
4. The channel row carries a placeholder chat id; the real value must be
   confirmed by the owner at deploy time and is never committed to this repo.

Target (this platform, per spec):

1. The workflow calls `POST /api/v1/posts` with a batch of universal posts.
2. UPP persists each post plus one publication task per (post, account).
3. UPP's scheduler assigns slots from `publishing_schedules` (IANA time zones,
   ISO weekdays) and converts them to UTC `scheduled_at`.
4. UPP publishes through platform adapters and records attempts.
5. After acceptance, n8n can go offline without blocking publication
   (spec principle 5, durable acceptance).

### Integration decision (owner confirmation requested in the pull request)

Two integration shapes were considered while planning:

| | A. UPP pulls the legacy queue; n8n pings a `/tick` endpoint | B. Push ingestion via `POST /api/v1/posts` (spec baseline) |
|---|---|---|
| Source of truth | legacy `language_learning` schema | UPP's own database |
| Platform independence | queue schema is Telegram-shaped | universal content model (spec §6) |
| Durability after n8n outage | partial | full (spec principle 5) |
| Retry ownership | split between systems | UPP owns retries and attempts |
| Multi-platform future | would need schema changes | accounts/platforms/adapters designed in |

**This plan follows the repository specification (B).** The push model
subsumes the benefits that motivated the pull idea (single source of truth,
retries in one place, independence from n8n at runtime). Per `AGENTS.md` §10,
changing this baseline would require explicit approval — the pull request
therefore asks the owner to confirm B (or request A).

## 2. Roadmap

Each step is one pull request: focused diff, tests green, lint clean,
documentation aligned. The final step touches n8n under the n8n-bot workflow
safety rules (fetch live JSON, minimal targeted diff, backup, diff gate,
verify read-back, active state preserved).

### Step 1 — scaffold (this pull request)

- Package layout per ARCHITECTURE §7, with only cohesive modules created
  (no empty stubs, per ARCHITECTURE §7).
- `core/config.py`: pydantic-settings `Settings` covering every variable in
  `.env.example` / spec §14; secrets as `SecretStr`; numeric sanity checks;
  `validate_production()` for service startup, so the CLI works without
  service-only secrets.
- `core/content.py`: universal content models — batch envelope (1–10 posts),
  post items, targets, media descriptors — validated per spec §6.3.
- `core/adapters.py`: typed `PlatformAdapter` protocol, `AccountConfig`
  (credentials masked), `PublishResult`, `AdapterError` with classification
  (`temporary` / `rate_limited` / `permanent`) — ARCHITECTURE §6 contract.
- `db/models.py`: SQLAlchemy 2.x models for `posts`, `platforms`, `accounts`,
  `publishing_schedules`, `publication_tasks`, `publication_attempts` with all
  uniqueness constraints (spec §8) and a task-status CHECK constraint.
- `db/session.py`: async engine/session factory; SQLite foreign keys enforced
  via pragma; `StaticPool` for in-memory databases.
- `db/repositories/tasks.py`: read-only `list_tasks(status, limit)` query.
- `publisher` CLI: `list-tasks` (alias `list-pending`) with `--status`,
  `--limit`, `--demo` (seeded in-memory database — runnable with no setup).
- pytest + ruff; all tests run on in-memory storage and isolated env.

Acceptance criteria: `pip install -e '.[dev]'` succeeds; `publisher list-tasks
--demo` prints tasks; `pytest` passes; `ruff check .` is clean; no secrets
committed; `README.md`, `.env.example`, and both docs are untouched.

### Step 2 — persistence migrations & ingestion service

- Alembic initialization and the initial migration creating all six tables.
- `services/ingestion.py` implementing spec §6: per-item processing,
  `(source, external_id)` idempotency, duplicate vs. conflict detection,
  target resolution against platforms/accounts, one task per valid target,
  itemized batch results.
- Unit tests against SQLite covering the ingestion items of spec §17.

Acceptance criteria: duplicate submissions are no-ops; conflicts reported per
item; invalid targets rejected without blocking valid ones; post + tasks
committed atomically per item.

> **Delivered (step-2 pull request):** Alembic initialized with an
> async-capable environment (`alembic/env.py` over `Base.metadata`,
> `DATABASE_URL` env override) and the initial migration `ed8835379d4c`
> creating all six tables with every §8 uniqueness constraint and the
> task-status CHECK (`alembic upgrade head` verified on an empty database,
> plus a downgrade/re-upgrade test). `services/ingestion.py` implements spec
> §6 per-item processing, `(source, external_id)` idempotency with
> duplicate/conflict classification, target resolution with per-item error
> codes (`unknown_platform`, `platform_disabled`, `unknown_account`,
> `inactive_account`), one `pending` task per valid target, and itemized
> results. 13 ingestion tests + 5 migration tests on SQLite; the REST
> endpoint and `X-API-Key` authentication remain step 3 as planned.

### Step 3 — REST API

- FastAPI application (`main.py`, `api/`), `/health` unauthenticated.
- `X-API-Key` authentication via `core/security.py` (constant-time compare).
- `POST /api/v1/posts` (batch 1–10), `GET /api/v1/posts/{post_id}`,
  `GET /api/v1/tasks` (filters + pagination), `GET /api/v1/tasks/{task_id}`.
- OpenAPI documentation; consistent error objects; no secrets in responses;
  startup calls `Settings.validate_production()`.

Acceptance criteria: the API items of spec §17 pass (key required, invalid key
rejected, batch boundaries, partial batch success, idempotency through the
API, persistence across restart).

> **Delivered (step-3 pull request):** FastAPI application (`main.py`, `api/`):
> unauthenticated `GET /health`; `X-API-Key` authentication via
> `core/security.py` (constant-time compare; one stable 401 error object for
> both missing and invalid keys); `POST /api/v1/posts` wired to the step-2
> ingestion service (batch 1–10 enforced by the request model; the itemized
> accepted/duplicate/conflict/rejected results are the response schema, spec
> §6.6); `GET /api/v1/posts/{post_id}` (post + per-target task statuses);
> `GET /api/v1/tasks` (status/platform/account/post-type filters, bounded
> `created_from`/`created_to` range, limit/offset pagination with totals);
> `GET /api/v1/tasks/{task_id}` (task + attempt history). Every non-2xx
> response uses the same `{"error": {code, message, details}}` object;
> OpenAPI is served at `/docs` and `/openapi.json`; startup calls
> `Settings.validate_production()`. No secrets, credential references, or
> account configuration appear in any response (`credentials_ref` and
> `configuration_json` are never mapped into API views). 24 API tests on
> in-memory and file-backed SQLite cover the §17 API items including
> persistence across a simulated restart; full suite 79 green, ruff clean.
> Run: `alembic upgrade head` then `uvicorn publisher.main:app` with `API_KEY`
> set (README "Running the API").

### Step 4 — scheduler & Telegram adapter

- APScheduler integration (`core/scheduling.py`): assign `scheduled_at` to
  pending tasks from account schedules; IANA time zones; ISO weekdays;
  documented DST policy for ambiguous/nonexistent local times (proposal:
  ambiguous → earlier offset; nonexistent → next valid instant; requires
  owner sign-off).
- Slot exclusivity: never two tasks in one account slot (spec §9.2).
- `adapters/telegram/`: `sendMessage`, HTML parse mode, chat id from account
  configuration, bot token via secret reference, `message_id` →
  `external_post_id`, error classification incl. `retry_after`, message
  length validation (spec §11.4).

Acceptance criteria: scheduling and adapter items of spec §17 pass; no
platform conditionals appear in core modules.

> **Delivered (step-4 pull request):** `core/scheduling.py` assigns
> `scheduled_at` to unscheduled `pending` tasks in one transaction: active
> schedules match by `(account, post_type)`, slots are computed from ISO
> weekdays (1=Monday…7=Sunday) in each schedule's IANA time zone and
> persisted as UTC (§9.1); tasks that already carry `scheduled_at` are
> never touched, and tasks without a matching schedule stay `pending`.
> DST policy implemented per the PLAN proposal (owner sign-off requested in
> the pull request): ambiguous local times → earlier offset; nonexistent
> local times → next valid instant (end of the gap, exact via offset
> binary search). Slot exclusivity (§9.2) is database-enforced by a unique
> `(account_id, scheduled_at)` index (migration `c4d8e21a95f0`; SQL NULL
> semantics leave unscheduled rows free) plus occupied-slot skipping inside
> the assignment transaction. `SchedulerService` wraps the pass in an
> APScheduler interval job (dependency added; wiring into app startup joins
> step 5 with the publishing engine). `adapters/telegram/` implements the
> `PlatformAdapter` protocol: `sendMessage` in HTML parse mode, chat id
> from account configuration, bot token via the account's credential
> reference (`SecretStr`, never logged — an httpx log filter redacts
> token-shaped URL segments), `message_id` → `external_post_id`, error
> classification with `retry_after` → rate-limited (invalid
> credentials/destination → permanent, transport/5xx → temporary), and
> 4096-character message-length validation before any request; media is
> rejected, not dropped (§12). All adapter tests run on a mocked httpx
> transport — no live Telegram calls. 39 new tests (DST resolution,
> scheduling §17 items, slot uniqueness at the database level, adapter
> behavior and secret hygiene); full suite 138 green, ruff clean. Core
> modules contain no platform references; the scheduling/adapter items of
> spec §17 pass except the missed-slot *relocation* of already-scheduled
> tasks, which belongs to the step-5 publishing engine per this plan.

### Step 5 — publishing engine & reliability

- `core/publishing.py`: atomic task claim + attempt record; due `scheduled`
  and `retry_wait` processing; success/retry/fail transitions (spec §9.3).
- Exponential backoff with jitter; `MAX_ATTEMPTS` default 5; rate-limit
  delays respected.
- Stale `publishing` recovery via `STALE_PUBLISHING_TIMEOUT_SECONDS`.
- Missed-slot downtime policy: skip missed slots, move pending work to future
  slots, log the rescheduling (spec §9.2).

Acceptance criteria: publishing/reliability items of spec §17 pass (claim
exclusivity, retries, max attempts, stale recovery, one target failing does
not affect another).

### Step 6 — deployment

- Dockerfile: non-root user, configurable port (default 8000), `/app/data`
  volume, `/health` healthcheck.
- `docker-compose.yml` example with external-network attachment;
  SIGTERM/SIGINT graceful shutdown; documented migration execution step.

Acceptance criteria: the same image and configuration model run in standalone
Docker and are TrueNAS SCALE Custom App ready (spec §15).

### Step 7 — migrate the live n8n workflow (final)

- Rework the n8n workflow so generation ends with a `POST /api/v1/posts` call
  instead of writing to `language_learning` and publishing via Telegram nodes.
- Executed under the n8n-bot safety rules: fetch live workflow JSON, minimal
  targeted diff, backup, structural diff gate, verify by read-back, active
  state preserved.
- The old publishing path is disabled (not deleted) until the owner confirms;
  a rollback plan is documented.

Acceptance criteria: one real post is published end-to-end by UPP; the n8n
diff is limited to the publishing handoff; rollback path verified.

## 3. Open decisions for the owner

1. Integration shape (§1): spec baseline (push ingestion) vs. the earlier
   pull/`/tick` idea. This plan assumes push; object in review to change.
2. DST policy for schedule calculation (step 4).
3. Default schedule time zone: the legacy system used `Asia/Yekaterinburg`;
   UPP stores an IANA name per schedule, defaulting to `UTC` if unset.
4. Account inventory: v1 target is the "English A1" Telegram channel; the
   current chat id is an unconfirmed placeholder and must be supplied by the
   owner at deploy time (never committed).
5. Post types: `dialogue` and `grammar` are confirmed; `tenses` and
   `vocabulary` are provisional. Unknown post types simply find no active
   schedule and stay `pending` (spec §9.1), which is a safe default.

## 4. Risks and documented limitations

- Ambiguous delivery: a crash after a platform accepted a request but before
  UPP committed success can duplicate a post on retry (ARCHITECTURE §8, spec
  §9.3/§10). Exactly-once delivery is not promised; attempts history records
  the ambiguity.
- SQLite single-process is the supported operating model for version 1
  (spec §5); no additional infrastructure is introduced without approval.
- Telegram rate limits: the adapter must honor `retry_after` (steps 4–5).
- The legacy `language_learning` tables remain untouched during steps 1–6 and
  become redundant only after step 7 is confirmed.
