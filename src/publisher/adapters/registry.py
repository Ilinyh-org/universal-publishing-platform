"""Adapter registry (specification §11.3).

Resolves a platform key to its registered adapter. Registrations are
explicit code (never dynamically executed from database values or user
input), one implementation per key, and unknown platforms are rejected
with a stable error.
"""

from __future__ import annotations

from ..core.adapters import PlatformAdapter


class UnknownPlatformError(LookupError):
    """No adapter is registered for the requested platform key."""

    def __init__(self, platform_key: str) -> None:
        self.platform_key = platform_key
        super().__init__(f"no adapter registered for platform '{platform_key}'")


class AdapterRegistry:
    """Explicit, code-driven registry of platform adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, PlatformAdapter] = {}

    def register(self, adapter: PlatformAdapter) -> None:
        """Register one adapter; duplicate keys are rejected."""
        key = adapter.platform_key
        if key in self._adapters:
            raise ValueError(f"an adapter is already registered for platform '{key}'")
        self._adapters[key] = adapter

    def resolve(self, platform_key: str) -> PlatformAdapter:
        """Return the adapter for `platform_key` or raise UnknownPlatformError."""
        try:
            return self._adapters[platform_key]
        except KeyError:
            raise UnknownPlatformError(platform_key) from None

    def is_registered(self, platform_key: str) -> bool:
        return platform_key in self._adapters

    def registered_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
