"""Telegram adapter implementation (specification §11.4).

The only Telegram-aware module in the codebase. Implements the common
`PlatformAdapter` protocol:

- `validate_account`: chat id present and integer-shaped in the account
  configuration; bot token resolvable from the account credentials.
- `validate_content`: text renders within Telegram's message-length limit;
  submitted media is rejected, never silently dropped (spec §12).
- `publish`: `sendMessage` in HTML parse mode; Telegram's `message_id`
  becomes `external_post_id`; responses and transport failures map to
  normalized `AdapterError`s with stable codes and classifications,
  including `retry_after` for rate limits (spec §10).

Secrets never appear in errors, results, or logs: exception text is reduced
to the exception class name, and only Telegram's own short `description`
field (bounded, token-free) is echoed in failure messages.
"""

from __future__ import annotations

import html
from collections.abc import Callable
from datetime import timedelta

import httpx

from ...core.adapters import (
    AccountConfig,
    AdapterError,
    ErrorClassification,
    PublishResult,
)
from ...core.content import UniversalContent
from .client import TelegramApiResponse, TelegramClient

PLATFORM_KEY = "telegram"

#: Bot API `sendMessage` `text` limit, in characters after entity parsing.
MAX_MESSAGE_CHARS = 4096

#: Upper bound on the Telegram `description` echoed in error messages.
_MAX_DESCRIPTION_CHARS = 200


class TelegramAdapter:
    """Common adapter interface, Telegram implementation."""

    platform_key = PLATFORM_KEY

    #: Credential fields this adapter expects in `AccountConfig.credentials`.
    credential_names = ("bot_token",)

    def __init__(
        self,
        client_factory: Callable[[str], TelegramClient] | None = None,
    ) -> None:
        """`client_factory` builds the HTTP client from a bot token; the
        default opens a real `TelegramClient`. Tests inject a factory over
        a mocked transport."""
        self._client_factory = client_factory or (lambda token: TelegramClient(token))

    async def validate_account(self, account: AccountConfig) -> None:
        """Raise `AdapterError` (permanent) if the account is unusable."""
        self._bot_token(account)
        self._chat_id(account)

    async def validate_content(
        self, content: UniversalContent, account: AccountConfig
    ) -> None:
        """Raise `AdapterError` if the content cannot be delivered as-is."""
        self._render_html(content)

    async def publish(
        self, content: UniversalContent, account: AccountConfig
    ) -> PublishResult:
        """Deliver one text post and normalize the outcome."""
        token = self._bot_token(account)
        chat_id = self._chat_id(account)
        text = self._render_html(content)
        client = self._client_factory(token)
        try:
            response = await client.send_message(chat_id, text)
        except httpx.TransportError as exc:
            # Class name only: exception text can embed request details.
            raise AdapterError(
                "network_error",
                f"Telegram request failed: {type(exc).__name__}",
                ErrorClassification.TEMPORARY,
            ) from exc
        finally:
            await client.aclose()
        return self._interpret(response)

    # -- account and content handling -------------------------------------

    def _bot_token(self, account: AccountConfig) -> str:
        credentials = account.credentials or {}
        secret = credentials.get("bot_token")
        value = secret.get_secret_value() if secret is not None else ""
        if not value.strip():
            raise AdapterError(
                "invalid_account",
                "account has no resolvable 'bot_token' credential",
                ErrorClassification.PERMANENT,
            )
        return value

    def _chat_id(self, account: AccountConfig) -> int:
        raw = account.configuration.get("chat_id")
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise AdapterError(
                "invalid_account",
                "account configuration requires an integer 'chat_id'",
                ErrorClassification.PERMANENT,
            ) from None

    def _render_html(self, content: UniversalContent) -> str:
        if content.media:
            raise AdapterError(
                "media_unsupported",
                "version 1 publishes text only; submitted media cannot be delivered",
                ErrorClassification.PERMANENT,
            )
        body = content.body or ""
        # HTML content is passed through as authored; everything else is
        # escaped so literal markup renders as text instead of entities.
        text = body if content.format == "html" else html.escape(body)
        title = (content.title or "").strip()
        rendered = f"<b>{html.escape(title)}</b>\n\n{text}" if title else text
        if not rendered.strip():
            raise AdapterError(
                "empty_content",
                "content renders to an empty message",
                ErrorClassification.PERMANENT,
            )
        if len(rendered) > MAX_MESSAGE_CHARS:
            raise AdapterError(
                "content_too_long",
                f"rendered message is {len(rendered)} characters; "
                f"Telegram allows at most {MAX_MESSAGE_CHARS}",
                ErrorClassification.PERMANENT,
            )
        return rendered

    # -- response interpretation -------------------------------------------

    def _interpret(self, response: TelegramApiResponse) -> PublishResult:
        if 200 <= response.status_code < 300 and response.payload.get("ok") is True:
            result = response.payload.get("result")
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if not isinstance(message_id, int):
                # Delivered, but the identifier is unusable: retry so the
                # attempt history records the ambiguity (spec §10).
                raise AdapterError(
                    "unexpected_response",
                    "sendMessage succeeded without a usable message_id",
                    ErrorClassification.TEMPORARY,
                )
            return PublishResult(external_post_id=str(message_id))
        raise self._error_from_response(response)

    def _error_from_response(self, response: TelegramApiResponse) -> AdapterError:
        payload = response.payload
        description = payload.get("description")
        if not isinstance(description, str):
            description = ""
        description = description[:_MAX_DESCRIPTION_CHARS]
        lowered = description.lower()
        parameters = payload.get("parameters")
        retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )

        if response.status_code == 429 or isinstance(retry_after, int):
            delay = (
                timedelta(seconds=retry_after)
                if isinstance(retry_after, int) and retry_after > 0
                else None
            )
            return AdapterError(
                "rate_limited",
                f"Telegram rate limit: {description or 'retry later'}",
                ErrorClassification.RATE_LIMITED,
                retry_after=delay,
            )
        if response.status_code == 401 or "unauthorized" in lowered:
            return AdapterError(
                "invalid_credentials",
                "Telegram rejected the bot token (401 Unauthorized)",
                ErrorClassification.PERMANENT,
            )
        if (
            "chat not found" in lowered
            or "chat_id is invalid" in lowered
            or "bot was blocked" in lowered
            or "group chat was upgraded" in lowered
        ):
            return AdapterError(
                "invalid_destination",
                f"Telegram destination problem: {description}",
                ErrorClassification.PERMANENT,
            )
        if "message is too long" in lowered:
            return AdapterError(
                "content_too_long",
                f"Telegram rejected the message length: {description}",
                ErrorClassification.PERMANENT,
            )
        if 400 <= response.status_code < 500:
            return AdapterError(
                "bad_request",
                f"Telegram rejected the request: {description or response.status_code}",
                ErrorClassification.PERMANENT,
            )
        return AdapterError(
            "platform_unavailable",
            f"Telegram is temporarily unavailable (HTTP {response.status_code})",
            ErrorClassification.TEMPORARY,
        )
