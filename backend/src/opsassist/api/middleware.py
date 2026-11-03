"""Pure ASGI middleware: correlation IDs, request logs, security headers, CSRF origin check."""

from __future__ import annotations

import json
import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from opsassist.observability import get_logger

log = get_logger("opsassist.http")

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


class RequestContextMiddleware:
    """Bind a request ID to logs and the response, and log each request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        supplied = Headers(scope=scope).get("x-request-id", "")
        request_id = supplied if _REQUEST_ID.match(supplied) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_headers(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            log.info(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )


class OriginCheckMiddleware:
    """CSRF defence for cookie auth, in addition to SameSite.

    Browsers attach Origin to cross-site unsafe requests. Non-browser clients send none and
    are not CSRF vectors, so an absent Origin is allowed unless Fetch Metadata says otherwise.
    """

    def __init__(self, app: ASGIApp, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed = {origin.rstrip("/") for origin in allowed_origins}

    def _allowed(self, headers: Headers) -> bool:
        origin = headers.get("origin")
        if origin is not None:
            return origin.rstrip("/") in self.allowed
        return headers.get("sec-fetch-site") != "cross-site"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["method"] not in _SAFE_METHODS
            and not self._allowed(Headers(scope=scope))
        ):
            body = json.dumps(
                {
                    "error": {
                        "code": "origin_not_allowed",
                        "message": "Request origin is not allowed",
                        "details": {},
                        "request_id": structlog.contextvars.get_contextvars().get("request_id"),
                    }
                }
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)
