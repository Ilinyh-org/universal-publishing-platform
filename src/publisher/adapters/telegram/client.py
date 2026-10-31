"""HTTP transport for the Telegram Bot API (specification §11.4).

A deliberately thin async wrapper: one bot token, one method
(`sendMessage`). It knows nothing about tasks, schedules, or persistence,
and it never raises on HTTP error statuses — the adapter owns response
interpretation and error classification (spec §11.2).

The bot token is carried only inside this instance (and the underlying
`httpx.AsyncClient`); it is never logged, never embedded in raised errors,
and never serialized (spec §13). Tests inject an `httpx` transport via
`transport=` — no test ever touches the real Telegram API.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

TELEGRAM_BASE_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT_SECONDS = 10.0

# httpx logs each request line ("HTTP Request: POST <url>") at INFO. Bot API
# URLs embed the token in the path, so a logging filter redacts any
# Telegram-token-shaped path segment before it can reach any handler
# (spec §13: tokens never in logs).
_BOT_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_\-]+/")


class _RedactBotTokenFilter(logging.Filter):
    """Redact `/bot<token>/` path segments from log records."""

    @staticmethod
    def _redact(value: Any) -> Any:
        if isinstance(value, str):
            return _BOT_TOKEN_IN_URL.sub("/bot***/", value)
        if isinstance(value, httpx.URL):
            return httpx.URL(_BOT_TOKEN_IN_URL.sub("/bot***/", str(value)))
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _BOT_TOKEN_IN_URL.sub("/bot***/", record.msg)
        if record.args:
            record.args = tuple(self._redact(arg) for arg in record.args)
        return True


def _install_redaction_filter() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(f, _RedactBotTokenFilter) for f in logger.filters):
        logger.addFilter(_RedactBotTokenFilter())


@dataclass
class TelegramApiResponse:
    """Raw Bot API outcome: HTTP status plus the parsed (or empty) body.

    Kept as plain data so the adapter can classify every shape — `ok`,
    `error_code`, `description`, `parameters.retry_after`, or a non-JSON
    body — without re-parsing.
    """

    status_code: int
    payload: dict[str, Any] = field(default_factory=dict)


class TelegramClient:
    """Async Bot API client for exactly one bot token."""

    def __init__(
        self,
        bot_token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = TELEGRAM_BASE_URL,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("bot token must be a non-empty string")
        self._token = bot_token
        _install_redaction_filter()
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=timeout_seconds,
        )

    async def send_message(self, chat_id: int, text: str) -> TelegramApiResponse:
        """Call `sendMessage` with HTML parse mode and return raw outcome."""
        response = await self._http.post(
            f"/bot{self._token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return TelegramApiResponse(
            status_code=response.status_code,
            payload=payload if isinstance(payload, dict) else {},
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    async def __aenter__(self) -> TelegramClient:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()
