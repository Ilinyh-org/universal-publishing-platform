"""`/health` — the only unauthenticated endpoint (specification §7, §16)."""

from __future__ import annotations

from fastapi import APIRouter

from .schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe: a simple status, no credentials or configuration."""
    return HealthResponse(status="ok")
