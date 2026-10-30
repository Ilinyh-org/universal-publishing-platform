"""Universal content model — one platform-independent input format (spec §6).

The core and adapters share these types. Original universal content is
preserved as received; platform-specific transformations happen inside
adapters at publication time only (spec §11).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MediaItem(BaseModel):
    """Media descriptor.

    Version 1 publishes text only; adapters that cannot handle submitted
    media must reject the target clearly instead of silently dropping it
    (spec §12).
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(min_length=1)
    url: str | None = None


class UniversalContent(BaseModel):
    """Platform-independent content, preserved as received after validation."""

    type: Literal["text"] = "text"
    title: str | None = None
    body: str | None = None
    format: str = "plain_text"
    media: list[MediaItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _text_requires_body(self) -> UniversalContent:
        if self.type == "text" and not (self.body and self.body.strip()):
            raise ValueError("text content requires a non-empty 'body'")
        return self


class PostTarget(BaseModel):
    """Destination resolved by `(platform, account)` at ingestion time."""

    platform: str = Field(min_length=1)
    account: str = Field(min_length=1)


class PostSubmission(BaseModel):
    """One item of an ingestion batch (spec §6.2)."""

    external_id: str = Field(min_length=1)
    content: UniversalContent
    targets: list[PostTarget] = Field(min_length=1)
    post_type: str = Field(min_length=1)
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None


class PostBatchRequest(BaseModel):
    """Batch envelope for `POST /api/v1/posts` (spec §6.1: 1–10 posts)."""

    source: str = Field(min_length=1)
    posts: list[PostSubmission] = Field(min_length=1, max_length=10)
