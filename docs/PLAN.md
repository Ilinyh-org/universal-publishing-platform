# Delivery Plan — Universal Publishing Platform

**Status:** Step 1 of the step-by-step build, delivered as the step-1 pull request.
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
