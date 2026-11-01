"""Secret resolution and account configuration assembly (specification §11.2, §13).

The core never stores or logs raw credentials. Accounts carry a
`credentials_ref` — the name of an environment variable or mounted secret —
and the secret provider resolves that reference to the named credential
fields the target adapter declares (`PlatformAdapter.credential_names`).
Platform-specific field names (e.g. which credential is a bot token) live
in the adapter, not here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from .adapters import AccountConfig, PlatformAdapter


@runtime_checkable
class SecretProvider(Protocol):
    """Resolves a credential reference to the adapter's named credentials.

    Returns `None` when the reference cannot be resolved; the adapter then
    rejects the account with a clear, classified error.
    """

    def resolve(
        self, credentials_ref: str, credential_names: Sequence[str] = ()
    ) -> Mapping[str, SecretStr] | None: ...


class StaticSecretProvider:
    """Mapping-backed provider for tests and explicit composition.

    Keys are credential references; values are the credential mappings
    handed to `AccountConfig` verbatim.
    """

    def __init__(self, secrets: Mapping[str, Mapping[str, SecretStr]]) -> None:
        self._secrets = dict(secrets)

    def resolve(
        self, credentials_ref: str, credential_names: Sequence[str] = ()
    ) -> Mapping[str, SecretStr] | None:
        return self._secrets.get(credentials_ref)


class EnvSecretProvider:
    """Reads secrets from the environment (specification §13).

    With exactly one declared credential field, the referenced environment
    variable is used directly (`TELEGRAM_BOT_TOKEN` style). With several
    fields, each is read from `<REF>_<FIELD>` (uppercase). Undeclared field
    names fall back to keying the mapping by the reference itself.
    """

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ if environ is not None else os.environ

    def resolve(
        self, credentials_ref: str, credential_names: Sequence[str] = ()
    ) -> Mapping[str, SecretStr] | None:
        direct = self._environ.get(credentials_ref)
        names = tuple(credential_names or ())
        if len(names) == 1 and direct is not None:
            return {names[0]: SecretStr(direct)}
        resolved: dict[str, SecretStr] = {}
        for name in names:
            value = self._environ.get(f"{credentials_ref}_{name.upper()}")
            if value is not None:
                resolved[name] = SecretStr(value)
        if resolved:
            return resolved
        if direct is not None and not names:
            return {credentials_ref: SecretStr(direct)}
        return None


def adapter_credential_names(adapter: PlatformAdapter) -> tuple[str, ...]:
    """Credential field names declared by an adapter (empty when none)."""
    names = getattr(adapter, "credential_names", ())
    return tuple(names) if names else ()


def build_account_config(
    *,
    account_key: str,
    platform_key: str,
    configuration: Mapping[str, object],
    credentials_ref: str,
    adapter: PlatformAdapter,
    secrets: SecretProvider,
) -> AccountConfig:
    """Assemble the adapter-facing account view (specification §11.2).

    `configuration` carries only non-secret account settings; credentials
    are resolved through the secret provider and excluded from reprs by
    `AccountConfig`.
    """
    return AccountConfig(
        key=account_key,
        platform=platform_key,
        configuration=dict(configuration),
        credentials=secrets.resolve(credentials_ref, adapter_credential_names(adapter)),
    )
