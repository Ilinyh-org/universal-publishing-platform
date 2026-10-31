"""Telegram Bot API adapter (specification §11.4) — version 1.

Text posts only, delivered with `sendMessage` in HTML parse mode. The chat
id comes from the account configuration, the bot token from the account's
resolved credentials (a secret reference resolved by the secret provider,
never stored in the database and never logged). Telegram's `message_id`
becomes `external_post_id`; errors are classified per spec §10, including
`retry_after` handling for rate limits, and message length is validated
against the Bot API limit before any request is sent.
"""

from .adapter import MAX_MESSAGE_CHARS, PLATFORM_KEY, TelegramAdapter
from .client import TelegramApiResponse, TelegramClient

__all__ = [
    "MAX_MESSAGE_CHARS",
    "PLATFORM_KEY",
    "TelegramAdapter",
    "TelegramApiResponse",
    "TelegramClient",
]
