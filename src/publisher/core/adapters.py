"""Platform adapter contract (ARCHITECTURE §6, spec §11).

The core communicates with adapters only through this typed interface.
Platform-specific validation, formatting, API calls, and error
interpretation live exclusively in adapter packages.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .content import UniversalContent


class ErrorClassification(StrEnum):
    """How the publishing engine should treat an adapter failure (spec §10)."""

    TEMPORARY = "temporary"
    RATE_LIMITED = "rate_limited"
    PERMANENT = "permanent"


class AdapterError(Exception):
    """Normalized adapter failure with a stable code and classification.

    `message` must be safe to log and store (no tokens, headers, or raw
    platform responses).
    """

    def __init__(
        self,
        code: str,
        message: str,
        classification: ErrorClassification,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.classification = classification
        self.retry_after = retry_after


class AccountConfig(BaseModel):
    """Account identity, non-secret configuration, and credentials resolved
    by the secret provider. Credentials are excluded from reprs."""

    model_config = ConfigDict()

    key: str = Field(min_length=1)
    platform: str = Field(min_length=1)
    configuration: dict[str, Any] = Field(default_factory=dict)
    credentials: Mapping[str, SecretStr] | None = Field(default=None, repr=False)


class PublishResult(BaseModel):
    """Normalized publish outcome. `metadata` must stay sanitized."""

    external_post_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Common adapter interface; one implementation per platform key."""

    platform_key: str

    async def validate_account(self, account: AccountConfig) -> None:
        """Raise AdapterError if the account configuration is invalid."""
        ...

    async def validate_content(
        self, content: UniversalContent, account: AccountConfig
    ) -> None:
        """Raise AdapterError if the content cannot be published as submitted."""
        ...

    async def publish(
        self, content: UniversalContent, account: AccountConfig
    ) -> PublishResult:
        """Deliver the content and return a normalized result."""
        ...
