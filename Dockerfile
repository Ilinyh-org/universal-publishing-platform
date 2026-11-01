# Universal Publishing Platform — portable image (specification §15).
#
# One image serves every supported environment (standalone Docker, TrueNAS
# SCALE Custom Apps). All configuration arrives at runtime through
# environment variables (specification §14) — no secrets are copied into or
# built into the image (AGENTS.md §2). The persistent data directory is
# /app/data, intended to be a mounted volume.

# --- Stage 1: build the package into an isolated virtual environment -----
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
# README.md is part of the package metadata (pyproject `readme`).
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# --- Stage 2: minimal runtime ---------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="universal-publishing-platform" \
      org.opencontainers.image.description="Self-hosted publishing service: batch ingestion, scheduling, platform adapters" \
      org.opencontainers.image.source="https://github.com/Ilinyh-org/universal-publishing-platform" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    # Application port, default 8000 (specification §14). Overridable at
    # runtime (`docker run -e APP_PORT=…`); see docs/DEPLOYMENT.md.
    APP_PORT=8000 \
    # Persistent storage defaults (specification §15).
    DATA_DIR=/app/data \
    DATABASE_URL=sqlite+aiosqlite:////app/data/publisher.db

WORKDIR /app

# Dependencies and the application itself.
COPY --from=builder /opt/venv /opt/venv
# alembic.ini + alembic/ support the documented migration step
# `alembic upgrade head` inside the same image (specification §15).
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Dedicated non-root runtime user; /app/data is the volume mount point, so
# its ownership is baked in and named volumes inherit it.
RUN groupadd --system --gid 10001 upp \
    && useradd --system --uid 10001 --gid upp --home-dir /app --shell /usr/sbin/nologin upp \
    && mkdir -p /app/data \
    && chown upp:upp /app/data \
    && chmod 0755 /usr/local/bin/entrypoint.sh \
    && chmod -R go-w /app

# Informational default; the actual bind follows APP_PORT at runtime
# (docs/DEPLOYMENT.md: "Configurable port").
EXPOSE ${APP_PORT}

# SQLite database and persistent application state live here (specification §15).
VOLUME ["/app/data"]

# Non-root runtime (specification §15).
USER upp

# Signals reach uvicorn directly (exec-form entrypoint): SIGTERM/SIGINT stop
# new work, drain the in-flight engine tick, and close database resources.
ENTRYPOINT ["entrypoint.sh"]

# /health is unauthenticated and discloses no configuration (specification §16).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('APP_PORT', '8000') + '/health', timeout=4)"]
