"""Universal content model tests (spec §6.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from publisher.core.content import PostBatchRequest, PostSubmission


def _item(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "external_id": "abc-123",
        "content": {"type": "text", "body": "Hello!"},
        "targets": [{"platform": "telegram", "account": "english_channel"}],
        "post_type": "dialogue",
    }
    base.update(overrides)
    return base


def test_valid_text_item() -> None:
    item = PostSubmission.model_validate(_item())
    assert item.content.body == "Hello!"
    assert item.content.format == "plain_text"
    assert item.content.media == []
    assert item.created_at is None
    assert item.metadata is None


def test_text_requires_body() -> None:
    with pytest.raises(ValidationError):
        PostSubmission.model_validate(_item(content={"type": "text"}))


def test_blank_body_rejected() -> None:
    with pytest.raises(ValidationError):
        PostSubmission.model_validate(_item(content={"type": "text", "body": "   "}))


def test_unknown_content_type_rejected() -> None:
    with pytest.raises(ValidationError):
        PostSubmission.model_validate(_item(content={"type": "photo", "body": "x"}))


def test_targets_required() -> None:
    with pytest.raises(ValidationError):
        PostSubmission.model_validate(_item(targets=[]))


def test_media_descriptors_accepted() -> None:
    item = PostSubmission.model_validate(
        _item(
            content={
                "type": "text",
                "body": "hi",
                "media": [{"type": "photo", "url": "https://example.com/a.jpg"}],
            }
        )
    )
    assert item.content.media[0].type == "photo"


def test_external_id_required() -> None:
    with pytest.raises(ValidationError):
        PostSubmission.model_validate(_item(external_id=""))


def test_batch_boundaries() -> None:
    batch = PostBatchRequest(source="n8n", posts=[_item() for _ in range(10)])
    assert len(batch.posts) == 10
    with pytest.raises(ValidationError):
        PostBatchRequest(source="n8n", posts=[])
    with pytest.raises(ValidationError):
        PostBatchRequest(source="n8n", posts=[_item() for _ in range(11)])
