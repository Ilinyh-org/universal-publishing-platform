# Universal Publishing Platform — Technical Specification

**Status:** Final baseline  
**Version:** 1.0  
**Audience:** Developers and AI coding agents

## 1. Purpose

Build a self-hosted, platform-independent publishing service. It receives content from source systems such as n8n, stores it durably, schedules publication, and publishes it to configured accounts through platform adapters.

The first supported platform is Telegram. The design must allow additional platforms to be added without rewriting the publishing engine, scheduler, database model, or REST API.

The service must run in ordinary Docker environments and in TrueNAS SCALE Custom Apps using the same application image and codebase.

## 2. Goals and non-goals

### 2.1 Goals

- Accept posts from external content-generation systems through a versioned REST API.
- Store accepted posts so that publisher operation does not depend on the source system remaining online.
- Publish one post to one or more configured platform accounts.
- Track each post/account publication independently.
- Schedule posts according to account-specific schedules and time zones.
- Retry temporary failures without duplicating successful deliveries unnecessarily.
- Preserve the original universal content and transform it only for a target platform at publication time.
- Provide clear status, error, and attempt history.
- Support containerized deployment with persistent storage and environment-based configuration.

### 2.2 Non-goals for version 1

- A web administration interface.
- A distributed multi-worker deployment.
- Redis, Celery, Kafka, or another message broker.
- Guaranteed exactly-once delivery to external platforms.
- Editing accepted post content through the ingestion API.
- Platform-specific content-generation logic inside the core.
- Support for every social platform in the first release.

## 3. Mandatory architectural principles

1. **Core independence:** The publishing engine, scheduler, persistence layer, and REST API must not contain platform-specific implementation logic.
2. **Adapter boundary:** All platform-specific validation, formatting, API calls, response parsing, and error interpretation belong in platform adapters.
3. **One universal input model:** Source systems submit a platform-independent content representation and a list of targets.
4. **Independent delivery:** Every post/account pair is a separate publication task with its own state, retries, schedule, and result.
5. **Durable acceptance:** Once the API reports a post as accepted, its content and publication tasks are persisted. The publisher must be able to continue if n8n is offline.
6. **Configuration, not environment assumptions:** Do not hardcode host paths, IP addresses, Docker network names, ports, or TrueNAS-specific behavior.
7. **No silent data loss:** Invalid items, duplicates, and partial failures must be reported explicitly.
8. **UTC persistence:** Store instants in UTC. Store the IANA time-zone name used to calculate schedules.
9. **Explicit state transitions:** Task status changes must be validated and persisted atomically.
10. **No platform conditionals in core:** Do not scatter checks such as `if platform == "telegram"` through core services. Resolve a platform through the adapter registry.

## 4. Technology stack

Use the following stack for version 1:

- Python 3.12 or newer, pinned to a supported minor version in the Docker build.
- FastAPI for the REST API.
- Pydantic for request/response validation and settings.
- SQLAlchemy 2.x for persistence.
- Alembic for database migrations.
- SQLite for the initial database.
- APScheduler for scheduling and periodic task processing.
- httpx for outbound HTTP requests.
- pytest and pytest-asyncio for tests.
- Docker and Docker Compose for packaging and local deployment.

Use asynchronous I/O for API and adapter network calls. Keep database access patterns compatible with SQLite. Do not introduce additional infrastructure unless a documented requirement makes it necessary.

## 5. Runtime components

The application consists of:

- **REST API:** authentication, request validation, ingestion, and status queries.
- **Post service:** validates and stores incoming posts and creates target tasks.
- **Account service:** loads configured publishing accounts and their non-secret settings.
- **Adapter registry:** resolves a platform name to its adapter implementation.
- **Publishing engine:** claims due tasks, invokes adapters, and records outcomes.
- **Scheduler:** calculates publication times and wakes the publishing engine.
- **Persistence layer:** stores posts, accounts, schedules, tasks, and attempts.
- **Configuration/secrets layer:** loads API keys and platform credentials from environment variables or mounted secret files.

The scheduler and API run in the same application process in version 1. The implementation must prevent overlapping processing of the same task and must document that SQLite/single-process deployment is the supported initial operating model.

## 6. Universal content and ingestion contract

### 6.1 Batch endpoint

`POST /api/v1/posts`

- Requires API-key authentication.
- Accepts between 1 and 10 posts per request.
- Each item is validated and processed independently.
- The response reports the result for every submitted item.
- A failure for one item must not roll back valid, independent items in the same batch.
- The API must not report an item as accepted until its post and all requested publication tasks have been committed.

### 6.2 Request format

The request has a top-level `source` identifying the sending system and a `posts` array.

```json
{
  "source": "n8n",
  "posts": [
    {
      "external_id": "english-dialogue-000125",
      "content": {
        "type": "text",
        "title": "At the coffee shop",
        "body": "Can I have a coffee?\nSure!",
        "format": "plain_text",
        "media": []
      },
      "targets": [
        {
          "platform": "telegram",
          "account": "english_channel"
        }
      ],
      "post_type": "dialogue",
      "metadata": {
        "language": "en",
        "level": "A1"
      },
      "created_at": "2026-10-30T18:00:00Z"
    }
  ]
}
```

### 6.3 Field definitions

- `source`: required non-empty identifier for the upstream system. It is part of the idempotency key.
- `external_id`: required stable identifier unique within `source`.
- `content`: required universal content object. Preserve the submitted object as received after validation/normalization.
- `content.type`: required content category, initially `text`. The model must be extensible for media or mixed content.
- `content.title`: optional title.
- `content.body`: text content when applicable.
- `content.format`: source representation hint, such as `plain_text` or `markdown`. It is not a platform name.
- `content.media`: optional array of media descriptors. Version 1 may accept an empty array; unsupported media must be rejected clearly rather than silently discarded.
- `targets`: one or more destinations. Each target identifies a registered `platform` and configured `account`.
- `post_type`: required classification used by schedule matching, such as `dialogue`, `grammar`, or `vocabulary`. It is platform-independent.
- `metadata`: optional arbitrary JSON object for source-specific descriptive information. Do not use arbitrary metadata fields as hidden control fields.
- `created_at`: optional source creation timestamp in ISO 8601 format. If absent, use the publisher's receipt time for relevant operational timestamps.

A request may not specify publication time. Scheduling is controlled by the publisher's account schedule.

### 6.4 Target resolution

A target is resolved by `(platform, account)`. The account identifier is a stable configured account key, not a credential or token.

Reject a target if its platform adapter is not registered, the account does not exist, the account belongs to a different platform, or the account is inactive. Report the error against the relevant post item. Do not create a task for an invalid target.

If a post contains both valid and invalid targets, create tasks for valid targets and report the invalid targets. The post is accepted if at least one target task is successfully created. If no target is valid, the item is rejected.

### 6.5 Idempotency and duplicate handling

The unique idempotency key is `(source, external_id)`.

- A repeated request with the same key and semantically identical immutable post data is a duplicate; do not create another post or publication task.
- A repeated request with the same key but different immutable post data is a conflict. Return a conflict result for that item; do not overwrite the original post.
- Duplicate handling must not create duplicate tasks for the same post/account pair.
- Re-sending an accepted post must not reset task status, schedule, attempt count, or publication result.
- Database constraints must enforce uniqueness, not only application-level checks.

For version 1, the immutable data comparison includes `content`, `post_type`, and the normalized target set. Metadata and `created_at` should also be treated as immutable unless explicitly documented otherwise in the implementation.

### 6.6 Batch response

Return an itemized result, for example:

```json
{
  "results": [
    {
      "external_id": "english-dialogue-000125",
      "status": "accepted",
      "post_id": "publisher-generated-id",
      "created_tasks": 1,
      "invalid_targets": []
    }
  ]
}
```

Use an item status such as `accepted`, `duplicate`, `conflict`, or `rejected`. Include stable machine-readable error codes and human-readable messages for rejected targets/items. Choose an appropriate HTTP status for the batch envelope, but do not hide per-item outcomes in a single generic error. Document the exact response schema in OpenAPI and tests.

## 7. REST API

Minimum endpoints:

| Method | Path | Purpose | Authentication |
|---|---|---|---|
| GET | `/health` | Liveness/health response | No |
| POST | `/api/v1/posts` | Ingest a batch of posts | Yes |
| GET | `/api/v1/posts/{post_id}` | Retrieve post and target task statuses | Yes |
| GET | `/api/v1/tasks` | List/filter publication tasks | Yes |
| GET | `/api/v1/tasks/{task_id}` | Retrieve one task and its attempts | Yes |

The task-list endpoint should support practical filters including status, account, platform, post type, and a bounded date range. Add pagination. Do not expose credentials or secret values in any API response.

Use JSON responses and consistent error objects. Document request and response schemas in generated OpenAPI documentation. Do not add public unauthenticated endpoints other than health.

## 8. Database model

Use SQLAlchemy models and Alembic migrations. Use generated internal IDs (UUIDs or another consistent opaque identifier). Foreign keys must be enabled in SQLite.

### 8.1 `posts`

Stores the original accepted content.

Required fields:
- `id`
- `source`
- `external_id`
- `content_json`
- `post_type`
- `metadata_json`
- `source_created_at` (nullable)
- `received_at`
- `created_at`

Constraints:
- Unique `(source, external_id)`.
- Preserve original content; do not replace it with platform-transformed content.

### 8.2 `platforms`

Represents registered adapter types, not individual social accounts.

Required fields:
- `id`
- `key` (unique, e.g. `telegram`)
- `display_name`
- `enabled`

The code adapter registry is authoritative for implementation availability. Database rows may provide enable/disable/configuration metadata but must not permit arbitrary dynamic code loading.

### 8.3 `accounts`

Represents a configured destination account.

Required fields:
- `id`
- `platform_id`
- `key` (unique within a platform)
- `display_name`
- `credentials_ref`
- `configuration_json`
- `active`
- `created_at`
- `updated_at`

`credentials_ref` identifies a secret supplied outside the database, such as an environment variable name or mounted secret reference. Never store raw API tokens in this table.

### 8.4 `publishing_schedules`

Defines when an account may publish a given post type.

Required fields:
- `id`
- `account_id`
- `post_type`
- `publish_time` (local wall-clock time)
- `timezone` (IANA name, e.g. `Asia/Yekaterinburg`)
- `days_of_week` (ISO weekday numbers: Monday=1 through Sunday=7)
- `active`
- `created_at`
- `updated_at`

Allow multiple schedule slots for an account/post type. Validate time-zone names. Define and test behavior for daylight-saving transitions. If a local time is ambiguous or nonexistent, apply one documented deterministic policy and test it.

### 8.5 `publication_tasks`

One row per `(post, account)` destination. This is the operational unit of publishing.

Required fields:
- `id`
- `post_id`
- `account_id`
- `status`
- `scheduled_at` (UTC, nullable until assigned)
- `next_attempt_at` (UTC, nullable)
- `published_at` (UTC, nullable)
- `external_post_id` (nullable)
- `attempt_count`
- `last_error_code` (nullable)
- `last_error_message` (nullable, sanitized)
- `created_at`
- `updated_at`

Constraints:
- Unique `(post_id, account_id)`.
- Index fields used for due-task and status queries.

Statuses:
- `pending`: accepted and waiting for schedule assignment.
- `scheduled`: has a future or due publication time.
- `publishing`: currently claimed by the publishing engine.
- `retry_wait`: a temporary failure occurred; `next_attempt_at` controls the next attempt.
- `published`: successful delivery confirmed.
- `failed`: permanent failure or retry limit exhausted.
- `cancelled`: explicitly cancelled by an authorized operation.

### 8.6 `publication_attempts`

Append-only history of actual publish attempts.

Required fields:
- `id`
- `task_id`
- `attempt_number`
- `started_at`
- `finished_at` (nullable while in progress)
- `success`
- `error_code` (nullable)
- `error_message` (nullable, sanitized)
- `response_metadata_json` (nullable, sanitized)

Constraint: unique `(task_id, attempt_number)`. Never store access tokens, authorization headers, or sensitive response data in attempt records or logs.

## 9. Scheduling and task processing

### 9.1 Schedule assignment

- Match a task's `post_type` and account to an active `publishing_schedules` row.
- Assign the next eligible slot in the configured account time zone.
- Convert the resulting instant to UTC before storing it in `scheduled_at`.
- A task with an assigned `scheduled_at` must not be moved by routine scheduler runs.
- Schedule edits affect tasks that are still `pending` and have no assigned time. Do not silently reschedule already scheduled tasks.
- If no matching active schedule exists, keep the task `pending` and expose a clear status/error reason. Do not publish immediately as a fallback.

### 9.2 Missed slots and downtime

When the application starts after downtime, it must not publish every missed slot as a backlog. Skip missed schedule slots and assign future eligible slots to unscheduled `pending` tasks. Already scheduled tasks whose time passed during downtime require a documented policy: for version 1, mark the missed slot as skipped by moving the task to the next future eligible slot, rather than publishing a burst. Record this rescheduling event in logs or task history.

Never assign more than one post to the same account schedule slot. Enforce this with a transaction and suitable uniqueness/locking strategy.

### 9.3 Due-task processing

The engine periodically finds tasks that are due:
- `scheduled` with `scheduled_at <= now`, or
- `retry_wait` with `next_attempt_at <= now`.

It must claim a task atomically before publishing it. Concurrent scheduler ticks must not publish the same task simultaneously. Change the task to `publishing` and create an attempt record in a transaction.

On success:
- Set task status to `published`.
- Set `published_at`.
- Store `external_post_id` if returned by the platform.
- Clear retry/error scheduling fields as appropriate.
- Finish the attempt successfully and store sanitized response metadata.

On temporary failure:
- Set status to `retry_wait`.
- Set `next_attempt_at`.
- Record sanitized error information and finish the attempt.

On permanent failure:
- Set status to `failed`.
- Record the error and finish the attempt.

A process crash while a task is `publishing` must not leave it stuck forever. Implement a stale-claim recovery mechanism using a claim timestamp/attempt state and a configurable timeout. Recovery must account for ambiguous delivery: the external platform may have accepted a request even if the publisher crashed before recording success. Do not claim exactly-once delivery.

## 10. Retry policy and failure classification

Default maximum: **5 total attempts**, including the initial attempt.

Use exponential backoff with configurable base and maximum delay, plus jitter. Respect platform-provided retry delays such as Telegram `retry_after`; do not retry before the requested delay.

Adapters classify errors into:
- `temporary`: retry with backoff.
- `rate_limited`: retry no earlier than the platform-provided time.
- `permanent`: mark failed without retry.

Examples:
- Connection reset or temporary server error: usually temporary.
- Invalid token, invalid destination, unsupported content: permanent until configuration/content is corrected.
- Rate limit: rate-limited.
- Timeout after request transmission: delivery may be ambiguous. Record the ambiguity and apply a conservative retry policy. Document that a duplicate external post is possible because generic platform APIs do not provide universal idempotency.

Make retry limits and delays configurable. Do not allow one task's failure to block other tasks.

## 11. Adapter architecture

### 11.1 Strict core/adapter separation

The publishing engine, scheduler, database layer, and REST API must remain independent of individual platform implementations.

All platform-specific functionality must be implemented exclusively through adapters.

Adding a new platform must require:
1. Implementing the common adapter interface.
2. Registering the adapter.
3. Adding platform-specific account configuration and secret references.
4. Adding adapter tests and documentation.

Adding a platform must not require modifications to the core publishing engine, scheduler, database schema, or REST API unless a genuinely new platform-independent capability is required.

The core communicates with adapters only through the common interface. Do not place platform-specific conditionals throughout core code. Do not import platform SDKs into core modules.

### 11.2 Common adapter contract

Define a typed interface/protocol with methods equivalent to:

```python
class PlatformAdapter(Protocol):
    platform_key: str

    async def validate_account(self, account: AccountConfig) -> None: ...

    async def validate_content(
        self, content: UniversalContent, account: AccountConfig
    ) -> None: ...

    async def publish(
        self, content: UniversalContent, account: AccountConfig
    ) -> PublishResult: ...
```

Use typed shared result/error models:
- `AccountConfig`: account ID/key, non-secret configuration, and resolved credentials supplied securely by the secret provider.
- `UniversalContent`: platform-independent content model.
- `PublishResult`: external post/message identifier and sanitized response metadata.
- `AdapterError`: stable error code, safe message, classification (`temporary`, `rate_limited`, `permanent`), and optional retry delay.

If platform-specific transformation is needed, implement it inside the adapter or its private helper modules. The core passes universal content and receives a normalized result. The adapter must not modify the stored original content.

### 11.3 Registry

Adapters are registered by a stable key, for example `telegram`. The registry:
- Resolves the adapter for a platform key.
- Rejects unknown or disabled platforms clearly.
- Ensures one adapter implementation is registered per key.
- Does not dynamically execute code from database values or user input.

### 11.4 Telegram adapter — version 1

Implement Telegram using the official Bot API over HTTPS.

Initial scope:
- `sendMessage` for text posts.
- HTML parse mode support.
- Destination chat ID from account configuration.
- Bot token resolved from a secret reference, not stored in the database.
- Record Telegram's returned `message_id` as `external_post_id`.
- Parse Telegram errors and `retry_after`.
- Apply Telegram message length constraints and return a clear validation error if content cannot be published as submitted.

The adapter must perform all Telegram-specific formatting and API details. The core must not know about chat IDs, Bot API methods, parse modes, or Telegram response structures.

## 12. Media handling

Version 1 may support text-only publication. The universal model must include a media collection so the API contract can evolve.

Do not silently ignore media. If media is submitted to a target that does not support it, return a target-specific validation failure. Before implementing media support, define how media is referenced, fetched, validated, stored, and cleaned up. Do not assume a source system's private filesystem path is accessible inside the publisher container.

## 13. Authentication and secrets

- Protect all API endpoints except `/health` with an API key.
- Accept the key through the `X-API-Key` header.
- Store the publisher API key in environment configuration or a mounted secret.
- Compare credentials safely and never log them.
- Store platform tokens outside the database. Resolve them through a secret provider using `credentials_ref`.
- Never return secret values from APIs.
- Do not put secrets in source control, images, example files, or logs.
- Support secret rotation by changing the configured secret and restarting/reloading the service according to documented behavior.

The API is intended for a trusted private network. Do not expose it directly to the public internet without an additional security layer.

## 14. Configuration

All operational settings must be configurable through environment variables. Provide `.env.example` with placeholder values only.

At minimum support:

- `APP_ENV`
- `APP_HOST`
- `APP_PORT` (default `8000`)
- `DATABASE_URL` (default SQLite file under `/app/data`)
- `DATA_DIR` (default `/app/data`)
- `API_KEY`
- `SCHEDULER_ENABLED`
- `SCHEDULER_POLL_INTERVAL_SECONDS`
- `MAX_ATTEMPTS` (default `5`)
- `RETRY_BASE_DELAY_SECONDS`
- `RETRY_MAX_DELAY_SECONDS`
- `STALE_PUBLISHING_TIMEOUT_SECONDS`
- `LOG_LEVEL`
- Platform secret variables or mounted secret references, including Telegram bot token.

Use Pydantic Settings. Validate required production settings at startup. Never hardcode TrueNAS paths or Docker network names.

## 15. Docker and deployment requirements

- Build one portable Docker image for all supported deployment environments.
- Run as a non-root user.
- Expose configurable application port, default `8000`.
- Use `/app/data` as the default persistent data directory.
- Persist the SQLite database in a mounted volume.
- Include a Docker health check using `/health`.
- Use a restart policy such as `unless-stopped` in the Compose example.
- Handle SIGTERM/SIGINT gracefully: stop accepting new work, finish or safely release in-progress work, and close database/HTTP resources.
- Include a `docker-compose.yml` example and document how to attach the service to an existing external Docker network.
- Do not depend on a network name created by TrueNAS or any other platform.
- Ensure the same image and configuration model work in standalone Docker and TrueNAS SCALE Custom Apps.
- Include database migration execution as a documented startup/deployment step. Avoid running competing migrations from multiple app replicas.

## 16. Logging and observability

Use structured logs where practical. Include request/task/attempt identifiers and stable error codes.

Log:
- API request outcomes without secrets or full sensitive content.
- Post acceptance, duplicate, conflict, and rejection.
- Schedule assignment and rescheduling.
- Task claim, attempt start, success, retry, and failure.
- Adapter errors in sanitized form.
- Startup, shutdown, migration, and recovery events.

Do not log API keys, bot tokens, authorization headers, or raw secret configuration. `/health` should return a simple status and must not disclose credentials or internal configuration.

## 17. Testing requirements

Use automated tests. External platform APIs must be mocked; tests must not require a real Telegram bot.

Minimum test coverage:

### API and ingestion
- API key required and invalid key rejected.
- Batch size boundaries (1 and 10 accepted; 0 and 11 rejected).
- Per-item validation and partial batch success.
- Valid/invalid target combinations.
- Unknown platform/account and inactive account.
- Idempotent duplicate submission.
- Conflicting duplicate payload.
- No duplicate task for the same post/account.
- Accepted data persists after application restart.

### Scheduling
- Matching by account and post type.
- Time-zone conversion and UTC persistence.
- Daylight-saving ambiguous/nonexistent times.
- Multiple schedule slots.
- No schedule leaves task pending.
- Existing scheduled tasks are not reassigned by routine runs.
- Downtime/missed-slot policy.
- No two tasks use the same account slot.

### Publishing and reliability
- Success records external ID and published status.
- Temporary failure schedules retry.
- Permanent failure stops retrying.
- Rate-limit retry delay is respected.
- Maximum attempt count enforced.
- One target failing does not affect another target.
- Stale `publishing` task recovery.
- Concurrent processing does not claim a task twice.
- Ambiguous timeout is recorded and handled according to policy.

### Adapter
- Telegram text/HTML formatting.
- Telegram response parsing.
- Invalid credentials/destination classification.
- Rate-limit response parsing.
- Message-length validation.
- No credentials appear in logs or persisted response metadata.

## 18. Documentation and deliverables

The repository must contain:
- `README.md`: purpose, prerequisites, setup, configuration, deployment, API overview, and operational commands.
- `docs/TECHNICAL_SPECIFICATION.md`: this document; authoritative requirements.
- `docs/ARCHITECTURE.md`: component boundaries, data flow, database relationships, and adapter contract.
- `AGENTS.md`: mandatory instructions for AI coding agents.
- `.env.example`: documented variable names and safe placeholders.
- Dockerfile and Compose example.
- Alembic migrations.
- Automated tests.

Update documentation when implementation changes. Any proposed change to a mandatory architectural principle requires explicit user approval before implementation.

## 19. Definition of done

Version 1 is complete when:

1. The service starts in Docker with persistent SQLite storage.
2. `/health` works and protected API endpoints enforce `X-API-Key`.
3. A batch of up to 10 posts can be accepted with itemized partial results.
4. Idempotency and conflict behavior work as specified.
5. Posts create independent tasks for configured targets.
6. Account/post-type schedules assign future publication times correctly.
7. Scheduler and task engine process due work without duplicate concurrent claims.
8. Telegram text publishing works through the adapter.
9. Successes, failures, retries, and attempt history persist across restarts.
10. Missed slots and stale publishing tasks follow the defined recovery policy.
11. Automated tests cover the required cases using mocked external APIs.
12. The same image can be deployed in standalone Docker and TrueNAS SCALE without code changes.
13. Documentation and example configuration are complete.
14. No platform-specific implementation leaks into the core.
