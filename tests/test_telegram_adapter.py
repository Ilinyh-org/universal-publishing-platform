"""Telegram adapter tests (roadmap step 4, specification §11.4/§17).

All Bot API traffic runs through a mocked `httpx` transport — no test ever
touches the real Telegram network. Covers §17 adapter items: text/HTML
formatting, response parsing, invalid credentials/destination
classification, rate-limit (`retry_after`) parsing, message-length
validation, and the guarantee that no credential ever appears in errors,
results, or logs.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from publisher.adapters.telegram import (
    MAX_MESSAGE_CHARS,
    PLATFORM_KEY,
    TelegramAdapter,
    TelegramClient,
)
from publisher.core.adapters import AccountConfig, AdapterError, ErrorClassification
from publisher.core.content import MediaItem, UniversalContent

FAKE_TOKEN = "123456789:AA-fake-token-for-tests"
FAKE_CHAT_ID = -1001234567890


def make_account(
    *,
    chat_id: Any = FAKE_CHAT_ID,
    token: str | None = FAKE_TOKEN,
) -> AccountConfig:
    return AccountConfig(
        key="english_channel",
        platform=PLATFORM_KEY,
        configuration={"chat_id": chat_id},
        credentials={"bot_token": SecretStr(token)} if token is not None else None,
    )


def make_content(
    *,
    body: str = "Hello from UPP",
    title: str | None = None,
    fmt: str = "plain_text",
    media: list[MediaItem] | None = None,
) -> UniversalContent:
    return UniversalContent(
        type="text", title=title, body=body, format=fmt, media=media or []
    )


def recording_handler(
    status_code: int = 200,
    payload: dict[str, Any] | None = None,
    *,
    raw_body: bytes | None = None,
    seen: dict[str, Any] | None = None,
):
    """Build an httpx mock transport handler that records the request."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            seen["headers"] = dict(request.headers)
        if raw_body is not None:
            return httpx.Response(status_code, content=raw_body)
        return httpx.Response(status_code, json=payload if payload is not None else {})

    return handler


def make_adapter(handler) -> TelegramAdapter:
    return TelegramAdapter(
        client_factory=lambda token: TelegramClient(
            token, transport=httpx.MockTransport(handler)
        )
    )


# --------------------------------------------------------------------------
# Account validation
# --------------------------------------------------------------------------


async def test_validate_account_accepts_well_formed_account() -> None:
    adapter = TelegramAdapter()
    await adapter.validate_account(make_account())


@pytest.mark.parametrize(
    ("chat_id", "token"),
    [
        (None, FAKE_TOKEN),
        ("not-a-number", FAKE_TOKEN),
        ("", FAKE_TOKEN),
        (FAKE_CHAT_ID, None),
        (FAKE_CHAT_ID, ""),
    ],
)
async def test_validate_account_rejects_bad_configuration(chat_id, token) -> None:
    adapter = TelegramAdapter()
    with pytest.raises(AdapterError) as excinfo:
        await adapter.validate_account(make_account(chat_id=chat_id, token=token))
    assert excinfo.value.code == "invalid_account"
    assert excinfo.value.classification is ErrorClassification.PERMANENT


def test_platform_key_is_stable() -> None:
    assert TelegramAdapter().platform_key == PLATFORM_KEY == "telegram"


# --------------------------------------------------------------------------
# Formatting (§17: text/HTML formatting, §11.4: HTML parse mode)
# --------------------------------------------------------------------------


async def test_publish_success_maps_message_id_and_request() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(
        200, {"ok": True, "result": {"message_id": 4242, "chat": {"id": FAKE_CHAT_ID}}}, seen=seen
    )
    result = await make_adapter(handler).publish(make_content(), make_account())

    assert result.external_post_id == "4242"
    assert result.metadata == {}
    assert seen["body"] == {
        "chat_id": FAKE_CHAT_ID,
        "text": "Hello from UPP",
        "parse_mode": "HTML",
    }
    assert seen["url"].endswith(f"/bot{FAKE_TOKEN}/sendMessage")
    assert seen["url"].startswith("https://api.telegram.org")


async def test_plain_text_body_is_html_escaped() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 1}}, seen=seen)
    await make_adapter(handler).publish(
        make_content(body="a < b & \"c\" > d"), make_account()
    )
    assert seen["body"]["text"] == "a &lt; b &amp; &quot;c&quot; &gt; d"


async def test_html_format_is_passed_through() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 1}}, seen=seen)
    await make_adapter(handler).publish(
        make_content(body="<b>bold</b>", fmt="html"), make_account()
    )
    assert seen["body"]["text"] == "<b>bold</b>"


async def test_title_is_rendered_bold_above_body() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 1}}, seen=seen)
    await make_adapter(handler).publish(
        make_content(title="Lesson 1", body="Body text"), make_account()
    )
    assert seen["body"]["text"] == "<b>Lesson 1</b>\n\nBody text"


async def test_numeric_string_chat_id_is_accepted() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 7}}, seen=seen)
    await make_adapter(handler).publish(
        make_content(), make_account(chat_id=" -100999 ")
    )
    assert seen["body"]["chat_id"] == -100999


# --------------------------------------------------------------------------
# Error classification (§17: response parsing, credentials/destination,
# rate-limit parsing; §10 classification)
# --------------------------------------------------------------------------


async def test_rate_limit_with_retry_after_is_parsed() -> None:
    handler = recording_handler(
        429,
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 42",
            "parameters": {"retry_after": 42},
        },
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    error = excinfo.value
    assert error.code == "rate_limited"
    assert error.classification is ErrorClassification.RATE_LIMITED
    assert error.retry_after == timedelta(seconds=42)


async def test_rate_limit_without_retry_after_still_classified() -> None:
    handler = recording_handler(
        429, {"ok": False, "error_code": 429, "description": "Too Many Requests"}
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.classification is ErrorClassification.RATE_LIMITED
    assert excinfo.value.retry_after is None


async def test_unauthorized_maps_to_invalid_credentials() -> None:
    handler = recording_handler(
        401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.code == "invalid_credentials"
    assert excinfo.value.classification is ErrorClassification.PERMANENT


async def test_chat_not_found_maps_to_invalid_destination() -> None:
    handler = recording_handler(
        400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.code == "invalid_destination"
    assert excinfo.value.classification is ErrorClassification.PERMANENT


async def test_server_error_is_temporary() -> None:
    handler = recording_handler(
        502, {"ok": False, "error_code": 502, "description": "Bad Gateway"}
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.code == "platform_unavailable"
    assert excinfo.value.classification is ErrorClassification.TEMPORARY


async def test_non_json_body_still_classified() -> None:
    handler = recording_handler(502, raw_body=b"<html>gateway error</html>")
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.classification is ErrorClassification.TEMPORARY
    assert "<html>" not in excinfo.value.message


async def test_unexpected_client_error_is_permanent() -> None:
    handler = recording_handler(
        400, {"ok": False, "error_code": 400, "description": "Bad Request: something odd"}
    )
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.code == "bad_request"
    assert excinfo.value.classification is ErrorClassification.PERMANENT


async def test_success_without_message_id_is_temporary() -> None:
    handler = recording_handler(200, {"ok": True, "result": {}})
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    assert excinfo.value.code == "unexpected_response"
    assert excinfo.value.classification is ErrorClassification.TEMPORARY


async def test_transport_failure_is_temporary_without_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom [detail with host]")

    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(make_content(), make_account())
    error = excinfo.value
    assert error.code == "network_error"
    assert error.classification is ErrorClassification.TEMPORARY
    assert "ConnectError" in error.message
    assert "boom" not in error.message  # exception text is never echoed


# --------------------------------------------------------------------------
# Content validation (§17: message-length validation; §12 media)
# --------------------------------------------------------------------------


async def test_oversized_message_rejected_without_network_call() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 1}}, seen=seen)
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(
            make_content(body="x" * (MAX_MESSAGE_CHARS + 1)), make_account()
        )
    assert excinfo.value.code == "content_too_long"
    assert excinfo.value.classification is ErrorClassification.PERMANENT
    assert str(MAX_MESSAGE_CHARS) in excinfo.value.message
    assert seen == {}  # nothing was sent


async def test_title_pushes_rendered_message_over_limit() -> None:
    adapter = TelegramAdapter()
    with pytest.raises(AdapterError) as excinfo:
        await adapter.validate_content(
            make_content(title="T" * 100, body="x" * (MAX_MESSAGE_CHARS - 50)),
            make_account(),
        )
    assert excinfo.value.code == "content_too_long"


async def test_message_at_limit_is_accepted_by_validation() -> None:
    adapter = TelegramAdapter()
    await adapter.validate_content(
        make_content(body="x" * MAX_MESSAGE_CHARS), make_account()
    )


async def test_media_is_rejected_not_dropped() -> None:
    seen: dict[str, Any] = {}
    handler = recording_handler(200, {"ok": True, "result": {"message_id": 1}}, seen=seen)
    with pytest.raises(AdapterError) as excinfo:
        await make_adapter(handler).publish(
            make_content(media=[MediaItem(type="photo", url="https://example.test/p.png")]),
            make_account(),
        )
    assert excinfo.value.code == "media_unsupported"
    assert excinfo.value.classification is ErrorClassification.PERMANENT
    assert seen == {}


# --------------------------------------------------------------------------
# Secret hygiene (§17: no credentials in logs or persisted metadata; §13)
# --------------------------------------------------------------------------


async def test_token_never_appears_in_logs_errors_or_results(
    caplog: pytest.LogCaptureFixture,
) -> None:
    seen: dict[str, Any] = {}
    handlers = [
        recording_handler(200, {"ok": True, "result": {"message_id": 5}}, seen=seen),
        recording_handler(401, {"ok": False, "error_code": 401, "description": "Unauthorized"}),
        recording_handler(
            429,
            {
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 3},
            },
        ),
    ]
    with caplog.at_level(logging.DEBUG):
        for handler in handlers:
            adapter = make_adapter(handler)
            try:
                result = await adapter.publish(make_content(), make_account())
            except AdapterError as error:
                assert FAKE_TOKEN not in error.message
                assert FAKE_TOKEN not in repr(error)
                result = None
            if result is not None:
                assert FAKE_TOKEN not in result.model_dump_json()
    assert FAKE_TOKEN not in caplog.text


def test_account_repr_hides_credentials() -> None:
    account = make_account()
    assert FAKE_TOKEN not in repr(account)
