"""Consistent error objects for every non-2xx response (specification §7).

Every error is `{"error": {"code": ..., "message": ..., "details": ...}}`
with stable machine-readable codes. Messages never include secrets or
configuration values; unhandled exceptions collapse to a generic 500.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
}


class ErrorDetail(BaseModel):
    """Body of one error response."""

    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """Envelope for every non-2xx response (specification §7)."""

    error: ErrorDetail


class ApiError(Exception):
    """An error with a stable code and HTTP status, rendered by the handler."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _payload(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict:
    return {"error": ErrorDetail(code=code, message=message, details=details).model_dump()}


def register_error_handlers(app: FastAPI) -> None:
    """Attach the uniform error handlers to the application."""

    @app.exception_handler(ApiError)
    async def _handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_payload(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Loc/msg/type only: never echo submitted payloads back.
        details = [
            {"loc": [str(part) for part in error.get("loc", [])],
             "msg": error.get("msg", ""),
             "type": error.get("type", "")}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_payload(
                "validation_error", "request validation failed", details
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, f"http_{exc.status_code}")
        message = exc.detail if isinstance(exc.detail, str) else "request failed"
        return JSONResponse(status_code=exc.status_code, content=_payload(code, message))

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # No internals in the response; the exception is logged by the server.
        return JSONResponse(status_code=500, content=_payload("internal_error", "internal error"))
