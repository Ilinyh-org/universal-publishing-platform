"""Adapter contract tests (ARCHITECTURE §6)."""

from __future__ import annotations

from datetime import timedelta

from pydantic import SecretStr

from publisher.core.adapters import (
    AccountConfig,
    AdapterError,
    ErrorClassification,
    PublishResult,
)


def test_adapter_error_carries_classification_and_delay() -> None:
    error = AdapterError(
        "rate_limited",
        "slow down",
        ErrorClassification.RATE_LIMITED,
        retry_after=timedelta(seconds=7),
    )
    assert error.code == "rate_limited"
    assert error.classification is ErrorClassification.RATE_LIMITED
    assert error.retry_after == timedelta(seconds=7)


def test_account_config_hides_credentials() -> None:
    account = AccountConfig(
        key="english_channel",
        platform="telegram",
        configuration={"chat_id": "42"},
        credentials={"bot_token": SecretStr("sekret-token")},
    )
    assert "sekret-token" not in repr(account)
    assert account.credentials is not None
    assert account.credentials["bot_token"].get_secret_value() == "sekret-token"


def test_publish_result_shape() -> None:
    result = PublishResult(external_post_id="99", metadata={"sanitized": True})
    assert result.external_post_id == "99"
    assert result.metadata == {"sanitized": True}
