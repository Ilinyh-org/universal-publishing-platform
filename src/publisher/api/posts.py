"""`/api/v1/posts` — batch ingestion and post retrieval (specification §6, §7)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..core.content import PostBatchRequest
from ..db.repositories.posts import get_post_with_tasks
from ..services.ingestion import BatchIngestionResult, ingest_batch
from .deps import SessionDep, SessionFactoryDep, require_api_key
from .errors import ApiError, ErrorResponse
from .schemas import PostDetailResponse, post_detail

router = APIRouter(prefix="/posts", tags=["posts"], dependencies=[Depends(require_api_key)])


@router.post(
    "",
    response_model=BatchIngestionResult,
    responses={401: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
async def ingest_posts(
    batch: PostBatchRequest,
    session_factory: SessionFactoryDep,
) -> BatchIngestionResult:
    """Ingest a batch of 1–10 universal posts.

    Each item is processed independently (specification §6.1): the response
    reports `accepted`, `duplicate`, `conflict`, or `rejected` per item, and
    one item's failure never rolls back another. Duplicate handling is a
    no-op keyed on `(source, external_id)`; conflicting immutable data is
    reported without overwriting the stored post.
    """
    return await ingest_batch(session_factory, batch)


@router.get(
    "/{post_id}",
    response_model=PostDetailResponse,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_post(
    post_id: str,
    session: SessionDep,
) -> PostDetailResponse:
    """Retrieve a post with its per-target publication task statuses."""
    post = await get_post_with_tasks(session, post_id)
    if post is None:
        raise ApiError(404, "not_found", "post not found")
    return post_detail(post)
