"""Configuration loading tests (spec §14)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from publisher.core.config import Settings


def test_defaults(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None)
    assert settings.app_env == "production"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8000
    assert settings.log_level == "INFO"
    assert settings.data_dir == Path("/app/data")
    assert settings.database_url == "sqlite+aiosqlite:////app/data/publisher.db"
    assert settings.api_key is None
    assert settings.scheduler_enabled is True
    assert settings.scheduler_poll_interval_seconds == 15
    assert settings.max_attempts == 5
    assert settings.retry_base_delay_seconds == 30
    assert settings.retry_max_delay_seconds == 3600
    assert settings.stale_publishing_timeout_seconds == 900
    assert settings.telegram_bot_token is None


def test_env_file_loading(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "APP_PORT=9000\n"
        "DATABASE_URL=sqlite+aiosqlite:///./local.db\n"
        "API_KEY=secret-key\n"
        "TELEGRAM_BOT_TOKEN=12345:TestToken\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.app_port == 9000
    assert settings.database_url == "sqlite+aiosqlite:///./local.db"
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "secret-key"
    assert settings.telegram_bot_token is not None


def test_invalid_port_rejected(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_port="not-a-port")


def test_out_of_range_port_rejected(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_port=70000)


def test_invalid_max_attempts_rejected(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_attempts=0)


def test_secrets_masked_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, telegram_bot_token="12345:SuperSecretTokenValue")
    assert isinstance(settings.telegram_bot_token, SecretStr)
    assert "SuperSecretTokenValue" not in repr(settings)


def test_validate_production_requires_api_key(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, app_env="production")
    with pytest.raises(ValueError, match="API_KEY"):
        settings.validate_production()


def test_validate_production_requires_bot_token_when_scheduler_on(
    clean_env: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, app_env="production", api_key="key")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        settings.validate_production()


def test_validate_production_ok(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(
        _env_file=None, app_env="production", api_key="key", telegram_bot_token="token"
    )
    settings.validate_production()


def test_validate_production_skipped_outside_production(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, app_env="development")
    settings.validate_production()
