"""Deployment artifact checks (roadmap step 6, specification §15).

No Docker daemon is required: these tests encode the §15 image/Compose
requirements structurally, so regressions in the deployment files fail in
CI. Runtime verification (build, `/health`, SIGTERM stop) remains an
owner-deploy-time step — see docs/DEPLOYMENT.md §8.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_dockerfile_runs_as_non_root_user() -> None:
    dockerfile = _read("Dockerfile")
    user_lines = re.findall(r"^USER\s+(\S+)", dockerfile, re.MULTILINE)
    assert user_lines, "Dockerfile must declare a non-root USER"
    assert user_lines[-1] not in {"root", "0"}, "runtime user must not be root"


def test_dockerfile_declares_volume_data_dir_and_default_port() -> None:
    dockerfile = _read("Dockerfile")
    assert "VOLUME [\"/app/data\"]" in dockerfile
    assert re.search(r"^ENV .*APP_PORT=8000", dockerfile, re.MULTILINE | re.DOTALL)
    assert re.search(r"^EXPOSE\s+\$\{APP_PORT\}", dockerfile, re.MULTILINE)


def test_dockerfile_healthcheck_hits_health() -> None:
    dockerfile = _read("Dockerfile")
    match = re.search(r"^HEALTHCHECK[^CMD]*CMD\s+(.+)$", dockerfile, re.MULTILINE)
    assert match, "Dockerfile must define a HEALTHCHECK"
    assert "/health" in match.group(1)


def test_dockerfile_carries_migrations_and_exec_entrypoint() -> None:
    dockerfile = _read("Dockerfile")
    # The documented migration step runs from the same image (spec §15).
    assert re.search(r"^COPY\s+alembic\s+\./alembic", dockerfile, re.MULTILINE)
    assert "alembic.ini" in dockerfile
    # exec-form entrypoint: runtime signals reach uvicorn directly.
    assert re.search(r'^ENTRYPOINT\s+\["', dockerfile, re.MULTILINE)


def test_entrypoint_execs_uvicorn_for_signal_handling() -> None:
    entrypoint = _read("docker/entrypoint.sh")
    assert re.search(r"^exec\s+uvicorn", entrypoint, re.MULTILINE), (
        "entrypoint must exec uvicorn so SIGTERM/SIGINT reach it as PID 1"
    )
    assert "--port" in entrypoint and "APP_PORT" in entrypoint


def test_dockerignore_keeps_secrets_and_local_data_out_of_the_image() -> None:
    dockerignore = _read(".dockerignore")
    for pattern in (".env", "data/", "*.db", ".venv/", ".git/"):
        assert pattern in dockerignore, f".dockerignore must exclude {pattern}"


def test_compose_example_meets_specification_15() -> None:
    compose = _read("docker-compose.yml")
    assert re.search(r"^    restart: unless-stopped$", compose, re.MULTILINE)
    assert re.search(r"env_file:\s*\.env", compose)
    assert re.search(r"app-data:/app/data", compose)
    # External-network attachment, never a platform-created name dependency.
    assert re.search(r"external:\s*true", compose)
    # Stop grace period above the graceful-shutdown bound (drain safety).
    assert re.search(r"stop_grace_period:\s*60s", compose)


def test_deployment_guide_documents_migrations_and_secrets() -> None:
    guide = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "alembic upgrade head" in guide, "migration step must be documented"
    assert "TELEGRAM_BOT_TOKEN" in guide and "API_KEY" in guide
    assert "TrueNAS" in guide and "external: true" in guide
    assert "Rollback" in guide


def test_deployment_files_contain_no_secret_values() -> None:
    """Placeholders/references only — no real-looking credentials (spec §14)."""
    artifacts = [
        _read("Dockerfile"),
        _read("docker/entrypoint.sh"),
        _read("docker-compose.yml"),
        (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8"),
    ]
    secret_shapes = [
        r"ghp_[A-Za-z0-9]{20,}",       # GitHub token
        r"xox[baprs]-[A-Za-z0-9-]+",   # Slack token
        r"\d{8,10}:[A-Za-z0-9_-]{30,}",  # Telegram bot token
        r"AKIA[0-9A-Z]{16}",           # AWS access key
    ]
    for artifact in artifacts:
        for shape in secret_shapes:
            assert re.search(shape, artifact) is None, (
                f"deployment artifact contains a token-shaped literal ({shape})"
            )
