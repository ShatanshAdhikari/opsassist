"""Domain errors. The API layer maps them onto the error envelope."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_failed"


class AuthenticationRequired(AppError):
    status_code = 401
    code = "authentication_required"


class PermissionDenied(AppError):
    status_code = 403
    code = "permission_denied"


class NotFound(AppError):
    """Also used for resources the caller may not know exist, so existence does not leak."""

    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class PayloadTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message, details={"retry_after_seconds": retry_after_seconds})
        self.retry_after_seconds = retry_after_seconds


class ServiceUnavailable(AppError):
    status_code = 503
    code = "service_unavailable"


class ToolUnavailable(AppError):
    """The call did not complete. Callers must not imply that live state was checked."""

    status_code = 503
    code = "tool_unavailable"


class ToolNotCallable(AppError):
    status_code = 409
    code = "tool_not_callable"


class ToolDefinitionChanged(AppError):
    status_code = 409
    code = "tool_definition_changed"
