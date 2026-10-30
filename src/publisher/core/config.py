"""Environment-driven configuration (specification §14).

Variable names match `.env.example` exactly. Secrets are `SecretStr` so
they never leak through reprs or logs (AGENTS.md §3).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All operational settings, loaded from the environment or a `.env` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "production"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    data_dir: Path = Path("/app/data")
    database_url: str = "sqlite+aiosqlite:////app/data/publisher.db"

    api_key: SecretStr | None = None

    scheduler_enabled: bool = True
    scheduler_poll_interval_seconds: int = 15

    max_attempts: int = 5
    retry_base_delay_seconds: int = 30
    retry_max_delay_seconds: int = 3600
    stale_publishing_timeout_seconds: int = 900

    telegram_bot_token: SecretStr | None = None

    @model_validator(mode="after")
    def _sane_numbers(self) -> Settings:
        if not 1 <= self.app_port <= 65535:
            raise ValueError("APP_PORT must be within 1..65535")
        if self.scheduler_poll_interval_seconds < 1:
            raise ValueError("SCHEDULER_POLL_INTERVAL_SECONDS must be >= 1")
        if self.max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS must be >= 1")
        if self.retry_base_delay_seconds < 0 or self.retry_max_delay_seconds < 0:
            raise ValueError("retry delays must be >= 0")
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError("RETRY_MAX_DELAY_SECONDS must be >= RETRY_BASE_DELAY_SECONDS")
        if self.stale_publishing_timeout_seconds < 1:
            raise ValueError("STALE_PUBLISHING_TIMEOUT_SECONDS must be >= 1")
        return self

    def validate_production(self) -> None:
        """Full startup validation for the API service.

        Deliberately NOT run on plain Settings construction: CLI inspection
        commands (e.g. `publisher list-tasks`) must work without the
        service-only secrets. The application startup (roadmap step 3)
        calls this before serving.
        """
        missing: list[str] = []
        if self.app_env == "production":
            if self.api_key is None:
                missing.append("API_KEY")
            if self.scheduler_enabled and self.telegram_bot_token is None:
                missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            raise ValueError(f"missing required production settings: {', '.join(missing)}")
