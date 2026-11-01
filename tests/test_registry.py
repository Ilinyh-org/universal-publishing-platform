"""Adapter registry and secret provider tests (specification §11.3, §13).

The registry resolves platform keys to adapters, rejects duplicates and
unknown platforms, and never executes code from data. Secret providers map
a `credentials_ref` to the credential names an adapter declares — no real
secrets, no network (AGENTS.md §5).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from publisher.adapters.registry import AdapterRegistry, UnknownPlatformError
from publisher.core.adapters import AccountConfig, PublishResult
from publisher.core.content import UniversalContent
from publisher.core.secrets import (
    EnvSecretProvider,
    StaticSecretProvider,
    adapter_credential_names,
    build_account_config,
)


class _StubAdapter:
    platform_key = "telegram"
    credential_names = ("bot_token",)

    async def validate_account(self, account: AccountConfig) -> None: ...
    async def validate_content(
        self, content: UniversalContent, account: AccountConfig
    ) -> None: ...
    async def publish(
        self, content: UniversalContent, account: AccountConfig
    ) -> PublishResult:
        return PublishResult()


class _OtherAdapter(_StubAdapter):
    platform_key = "mastodon"
    credential_names = ()


# --------------------------------------------------------------------------
# Registry (spec §11.3)
# --------------------------------------------------------------------------


def test_registry_resolves_registered_adapter() -> None:
    registry = AdapterRegistry()
    adapter = _StubAdapter()
    registry.register(adapter)
    assert registry.resolve("telegram") is adapter
    assert registry.is_registered("telegram")
    assert registry.registered_keys() == ("telegram",)


def test_registry_rejects_duplicate_key() -> None:
    registry = AdapterRegistry()
    registry.register(_StubAdapter())
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_StubAdapter())


def test_registry_rejects_unknown_platform() -> None:
    registry = AdapterRegistry()
    with pytest.raises(UnknownPlatformError, match="mastodon") as exc_info:
        registry.resolve("mastodon")
    assert exc_info.value.platform_key == "mastodon"


def test_registry_no_dynamic_code_execution() -> None:
    """Registration is code-driven only; resolving never imports or execs."""
    registry = AdapterRegistry()
    registry.register(_StubAdapter())
    assert not registry.is_registered("__import__('os')")


# --------------------------------------------------------------------------
# Secret providers (spec §13)
# --------------------------------------------------------------------------


def test_env_provider_single_credential_name() -> None:
    provider = EnvSecretProvider({"TELEGRAM_BOT_TOKEN": "123:abc"})
    resolved = provider.resolve("TELEGRAM_BOT_TOKEN", ("bot_token",))
    assert resolved is not None
    assert list(resolved) == ["bot_token"]
    assert resolved["bot_token"].get_secret_value() == "123:abc"


def test_env_provider_multi_credential_convention() -> None:
    provider = EnvSecretProvider({"SVC_TOKEN": "t", "SVC_USER": "u"})
    resolved = provider.resolve("SVC", ("token", "user"))
    assert resolved is not None
    assert {k: v.get_secret_value() for k, v in resolved.items()} == {
        "token": "t",
        "user": "u",
    }


def test_env_provider_undeclared_name_keys_by_reference() -> None:
    provider = EnvSecretProvider({"TELEGRAM_BOT_TOKEN": "123:abc"})
    resolved = provider.resolve("TELEGRAM_BOT_TOKEN", ())
    assert resolved is not None
    assert resolved["TELEGRAM_BOT_TOKEN"].get_secret_value() == "123:abc"


def test_env_provider_missing_reference_returns_none() -> None:
    provider = EnvSecretProvider({})
    assert provider.resolve("TELEGRAM_BOT_TOKEN", ("bot_token",)) is None


def test_static_provider_returns_mapping_verbatim() -> None:
    credentials = {"bot_token": SecretStr("x")}
    provider = StaticSecretProvider({"TELEGRAM_BOT_TOKEN": credentials})
    assert provider.resolve("TELEGRAM_BOT_TOKEN", ("bot_token",)) is credentials
    assert provider.resolve("OTHER", ("bot_token",)) is None


def test_adapter_credential_names_helper() -> None:
    assert adapter_credential_names(_StubAdapter()) == ("bot_token",)
    assert adapter_credential_names(_OtherAdapter()) == ()
    assert adapter_credential_names(object()) == ()


def test_build_account_config_masks_credentials() -> None:
    secrets = StaticSecretProvider(
        {"TELEGRAM_BOT_TOKEN": {"bot_token": SecretStr("123:abc")}}
    )
    config = build_account_config(
        account_key="english_channel",
        platform_key="telegram",
        configuration={"chat_id": "100"},
        credentials_ref="TELEGRAM_BOT_TOKEN",
        adapter=_StubAdapter(),
        secrets=secrets,
    )
    assert isinstance(config, AccountConfig)
    assert config.key == "english_channel"
    assert config.platform == "telegram"
    assert config.configuration == {"chat_id": "100"}
    assert "123:abc" not in repr(config)  # credentials excluded from reprs
    assert config.credentials is not None
    assert config.credentials["bot_token"].get_secret_value() == "123:abc"


def test_build_account_config_unresolvable_credentials_is_none() -> None:
    config: Any = build_account_config(
        account_key="a",
        platform_key="telegram",
        configuration={},
        credentials_ref="MISSING",
        adapter=_StubAdapter(),
        secrets=EnvSecretProvider({}),
    )
    assert config.credentials is None
