"""One error envelope for every failure. Internal detail never reaches the client."""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException

from opsassist.errors import AppError, RateLimited
from opsassist.observability import get_logger

log = get_logger("opsassist.errors")


def _envelope(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
                "request_id": structlog.contextvars.get_contextvars().get("request_id"),
            }
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = (
            {"Retry-After": str(exc.retry_after_seconds)} if isinstance(exc, RateLimited) else None
        )
        return _envelope(exc.status_code, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field paths and reasons only. Submitted values may be passwords, so never echo them.
        issues = [
            {"field": ".".join(str(part) for part in err["loc"]), "issue": err["msg"]}
            for err in exc.errors()
        ]
        return _envelope(422, "validation_failed", "Invalid request", {"issues": issues})

    @app.exception_handler(HTTPException)
    async def _http(_request: Request, exc: HTTPException) -> JSONResponse:
        return _envelope(exc.status_code, "http_error", str(exc.detail))

    async def _database(_request: Request, exc: Exception) -> JSONResponse:
        log.error("database_unavailable", error_type=type(exc).__name__)
        return _envelope(503, "service_unavailable", "The database is unavailable")

    app.add_exception_handler(OperationalError, _database)
    app.add_exception_handler(ConnectionError, _database)
    app.add_exception_handler(DBAPIError, _unexpected)

    @app.exception_handler(Exception)
    async def _fallback(request: Request, exc: Exception) -> JSONResponse:
        return await _unexpected(request, exc)


async def _unexpected(_request: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_exception", error_type=type(exc).__name__, exc_info=exc)
    return _envelope(500, "internal_error", "An internal error occurred")
