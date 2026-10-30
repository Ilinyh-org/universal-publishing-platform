# Instructions for AI Coding Agents

## 1. Read before acting

Before making any change, read:

1. `docs/TECHNICAL_SPECIFICATION.md`
2. `docs/ARCHITECTURE.md`

These documents define the required behavior and architecture. Treat them as mandatory project requirements, not suggestions.

## 2. Approval boundaries

- Do not change mandatory architectural principles without explicit user approval.
- Do not introduce platform-specific logic into the core.
- Do not make the publisher dependent on n8n being online after a post has been accepted.
- Do not change the independent publication-task model: one task per post/account pair.
- Do not store raw platform credentials in the database, source code, Docker image, logs, or API responses.
- Do not add infrastructure such as Redis, Celery, or PostgreSQL without explaining the requirement and obtaining approval.
- If the specification is ambiguous or contradictory, stop and ask a focused question rather than inventing a new requirement.

## 3. Implementation rules

- Keep the core publishing engine, scheduler, REST API, and persistence layer platform-independent.
- Put all platform-specific behavior in adapters.
- Communicate with adapters only through the shared typed interface.
- Do not use platform-specific conditionals throughout core code.
- Preserve original universal post content. Perform platform-specific transformations at publication time.
- Use database transactions for idempotency, ingestion, task claiming, and state transitions.
- Store timestamps in UTC and use IANA time-zone names for schedule calculations.
- Make configuration environment-driven. Never hardcode host paths, IP addresses, Docker network names, or TrueNAS-specific assumptions.
- Use Alembic migrations for schema changes.
- Keep secrets out of logs and test output.
- Use type hints and clear, cohesive modules.

## 4. Workflow for each task

1. Inspect the relevant code and tests.
2. Identify which requirements in the specification apply.
3. Propose a concise implementation plan before a broad or architectural change.
4. Implement the smallest coherent change.
5. Add or update tests.
6. Run relevant tests, then the full test suite when practical.
7. Run linting and formatting checks configured by the repository.
8. Review the diff for accidental changes, secrets, and violations of the architecture.
9. Update documentation if behavior, configuration, API contracts, or architecture changed.
10. Report what changed, tests run, and any remaining limitations.

## 5. Testing expectations

- Mock external platform APIs. Automated tests must not require real accounts or tokens.
- Test success and failure paths.
- Test duplicate and conflicting ingestion requests.
- Test multiple targets independently.
- Test scheduling, time zones, retries, stale-task recovery, and concurrent claims.
- Never weaken or delete a test merely to make a build pass. Explain and obtain approval before changing an established requirement.

## 6. Git and delivery

- Work on a feature branch.
- Do not push directly to protected integration or production branches.
- Do not commit secrets, local databases, virtual environments, caches, or generated runtime data.
- Keep commits focused and use descriptive messages.
- Do not claim a task is complete unless the relevant checks have actually passed.
- Do not silently change deployment configuration or destructive database behavior.

## 7. Definition of done

A task is complete only when:
- Its implementation matches the technical specification.
- Relevant automated tests pass.
- Lint/format checks pass, or any existing unrelated failures are clearly identified.
- Documentation is updated where needed.
- The final report lists files changed, checks run, and known limitations.
