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


def test_validate_production_requires_bot_token_when_engine_on(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """ENGINE_ENABLED (default true) also requires the platform token."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        api_key="key",
        scheduler_enabled=False,
    )
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        settings.validate_production()


def test_validate_production_ok_without_background_work(
    clean_env: pytest.MonkeyPatch,
) -> None:
    """No token needed when neither scheduler nor engine runs."""
    settings = Settings(
        _env_file=None,
        app_env="production",
        api_key="key",
        scheduler_enabled=False,
        engine_enabled=False,
    )
    settings.validate_production()


def test_validate_production_skipped_outside_production(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, app_env="development")
    settings.validate_production()


def test_secret_file_loading(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """API_KEY_FILE / TELEGRAM_BOT_TOKEN_FILE load values from files."""
    key_file = tmp_path / "api_key"
    key_file.write_text("file-secret-key\n", encoding="utf-8")
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("12345:FromFileToken\n\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        api_key_file=str(key_file),
        telegram_bot_token_file=str(token_file),
    )
    assert settings.api_key is not None and settings.api_key.get_secret_value() == "file-secret-key"
    assert settings.telegram_bot_token is not None
    assert settings.telegram_bot_token.get_secret_value() == "12345:FromFileToken"
    settings.validate_production()


def test_secret_file_takes_precedence_over_direct_env(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A configured *_FILE wins over the direct variable."""
    key_file = tmp_path / "api_key"
    key_file.write_text("file-wins\n", encoding="utf-8")
    settings = Settings(_env_file=None, api_key="direct-value", api_key_file=str(key_file))
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "file-wins"


def test_direct_env_still_works_without_file(clean_env: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, api_key="direct-value")
    assert settings.api_key is not None
    assert settings.api_key.get_secret_value() == "direct-value"


def test_missing_secret_file_is_a_clear_error(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="/definitely/not/a/real/path"):
        Settings(_env_file=None, api_key_file="/definitely/not/a/real/path")


def test_unreadable_secret_file_is_a_clear_error(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_dir = tmp_path / "secretdir"
    secret_dir.mkdir()
    with pytest.raises(ValidationError, match="could not be read"):
        Settings(_env_file=None, api_key_file=str(secret_dir))


def test_secret_file_values_never_leak_in_repr(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_file = tmp_path / "api_key"
    key_file.write_text("file-secret-value\n", encoding="utf-8")
    settings = Settings(_env_file=None, api_key_file=str(key_file))
    assert "file-secret-value" not in repr(settings)
