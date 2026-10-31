"""API-key verification (specification §13).

The configured key lives in `Settings.api_key` as a `SecretStr` and is
compared in constant time. Missing header, unconfigured server key, and
wrong key are deliberately indistinguishable to the caller: one stable
401 error object, no oracle for which part failed.
"""

from __future__ import annotations

import secrets

from pydantic import SecretStr


class ApiKeyError(Exception):
    """A request failed API-key authentication."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def verify_api_key(provided: str | None, configured: SecretStr | None) -> None:
    """Verify the `X-API-Key` header against the configured key.

    Constant-time comparison (`secrets.compare_digest`), so response timing
    does not leak how much of the key matched. Raises `ApiKeyError` when the
    header is absent, the server has no key configured, or the key is wrong.
    """
    if provided is None or configured is None:
        raise ApiKeyError("missing or invalid API key")
    if not secrets.compare_digest(
        provided.encode("utf-8"),
        configured.get_secret_value().encode("utf-8"),
    ):
        raise ApiKeyError("missing or invalid API key")
