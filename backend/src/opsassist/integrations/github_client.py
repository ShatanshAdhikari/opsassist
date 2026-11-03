"""GitHub App authentication and HTTP access.

Installation tokens are always requested down-scoped: read-only permissions, and only the
repositories this connection allows. So even an app granted more on GitHub, or a bug in tool code
above, cannot write or reach another repository with these tokens.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from opsassist.tools.base import (
    CredentialRejected,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnreachable,
)

READ_ONLY_PERMISSIONS = {
    "metadata": "read",
    "contents": "read",
    "issues": "read",
    "pull_requests": "read",
    "checks": "read",
}
_API_VERSION = "2022-11-28"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": _API_VERSION,
    "User-Agent": "opsassist",
}
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 540
_TOKEN_EARLY_REFRESH_SECONDS = 300


class GitHubNotFound(Exception):
    """Missing, or not visible to this token. GitHub does not distinguish the two."""


class GitHubRejected(Exception):
    """GitHub refused the request itself, for example a query it cannot parse."""


@dataclass(frozen=True)
class GitHubConnection:
    """Connection settings plus the decrypted key. Lives in memory for one operation."""

    connection_id: Any
    credential_id: Any
    app_id: str
    installation_id: int
    repositories: tuple[str, ...]
    private_key: str = ""

    def __repr__(self) -> str:
        # Keeps the key out of tracebacks and debug output.
        return f"GitHubConnection(connection_id={self.connection_id}, app_id={self.app_id})"


class InstallationTokens:
    """In-memory token cache. Tokens are never persisted."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[Any, ...], tuple[str, float]] = {}

    @staticmethod
    def _key(connection: GitHubConnection) -> tuple[Any, ...]:
        # Changing the key or the repository list must not reuse a token scoped to the old one.
        return (connection.connection_id, connection.credential_id, connection.repositories)

    def invalidate(self, connection: GitHubConnection) -> None:
        self._tokens.pop(self._key(connection), None)

    def forget_connection(self, connection_id: Any) -> None:
        for key in [key for key in self._tokens if key[0] == connection_id]:
            del self._tokens[key]

    async def get(self, http: httpx.AsyncClient, connection: GitHubConnection) -> str:
        key = self._key(connection)
        cached = self._tokens.get(key)
        if cached is not None and cached[1] > time.time():
            return cached[0]
        token, expires_at = await _exchange(http, connection)
        self._tokens[key] = (token, expires_at - _TOKEN_EARLY_REFRESH_SECONDS)
        return token


def _app_jwt(connection: GitHubConnection) -> str:
    now = int(time.time())
    try:
        return jwt.encode(
            {
                "iat": now - _JWT_BACKDATE_SECONDS,
                "exp": now + _JWT_LIFETIME_SECONDS,
                "iss": connection.app_id,
            },
            connection.private_key,
            algorithm="RS256",
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise CredentialRejected() from exc


async def _exchange(http: httpx.AsyncClient, connection: GitHubConnection) -> tuple[str, float]:
    names = sorted(name.split("/", 1)[1] for name in connection.repositories)
    try:
        response = await http.post(
            f"/app/installations/{connection.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {_app_jwt(connection)}"},
            json={"repositories": names, "permissions": READ_ONLY_PERMISSIONS},
        )
    except httpx.TimeoutException as exc:
        raise ProviderTimeout() from exc
    except httpx.TransportError as exc:
        raise ProviderUnreachable() from exc
    if response.status_code == 201:
        body = _parse_json(response.content)
        try:
            expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
            return str(body["token"]), expires_at
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderProtocolError() from exc
    if rate_limit := _rate_limit(response):
        raise rate_limit
    if response.status_code >= 500:
        raise ProviderUnreachable()
    # Bad key, revoked app, removed installation, a listed repository outside the installation, or
    # permissions the app lacks. All need an admin, and none improves with a retry.
    raise CredentialRejected()


def _parse_json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except ValueError as exc:
        raise ProviderProtocolError() from exc


def _rate_limit(response: httpx.Response) -> ProviderRateLimited | None:
    if response.status_code not in {403, 429}:
        return None
    headers = response.headers
    if retry_after := headers.get("retry-after"):
        if retry_after.isdigit():
            return ProviderRateLimited(float(retry_after))
        try:
            when = parsedate_to_datetime(retry_after).timestamp()
        except (TypeError, ValueError):
            return ProviderRateLimited(60.0)
        return ProviderRateLimited(max(0.0, when - time.time()))
    if headers.get("x-ratelimit-remaining") == "0":
        reset = headers.get("x-ratelimit-reset", "")
        if reset.isdigit():
            return ProviderRateLimited(max(0.0, int(reset) - time.time()))
        return ProviderRateLimited(60.0)
    # A 429 with no hint is still a rate limit. A 403 with no hint is a permission answer.
    return ProviderRateLimited(60.0) if response.status_code == 429 else None


def encode_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


class GitHubApi:
    """Authenticated GETs with error mapping and a response size limit."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        tokens: InstallationTokens,
        connection: GitHubConnection,
        *,
        max_response_bytes: int,
    ) -> None:
        self._http = http
        self._tokens = tokens
        self._connection = connection
        self._max_bytes = max_response_bytes

    async def check_authentication(self) -> None:
        await self._tokens.get(self._http, self._connection)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response, content = await self._send(path, params)
        if response.status_code == 401:
            # Token expired or revoked early. Try once with a fresh one.
            self._tokens.invalidate(self._connection)
            response, content = await self._send(path, params)
            if response.status_code == 401:
                raise CredentialRejected()
        if response.status_code == 200:
            return _parse_json(content)
        if rate_limit := _rate_limit(response):
            raise rate_limit
        if response.status_code in {403, 404, 410}:
            raise GitHubNotFound()
        if response.status_code == 422:
            raise GitHubRejected()
        if response.status_code >= 500:
            raise ProviderUnreachable()
        # Includes redirects, which are not followed: a renamed repository could otherwise lead
        # outside the allowlist without the check above seeing it.
        raise ProviderProtocolError()

    async def _send(self, path: str, params: dict[str, Any] | None) -> tuple[httpx.Response, bytes]:
        token = await self._tokens.get(self._http, self._connection)
        try:
            async with self._http.stream(
                "GET", path, params=params, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._max_bytes:
                        raise ProviderProtocolError()
                return response, bytes(content)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout() from exc
        except httpx.TransportError as exc:
            raise ProviderUnreachable() from exc


def new_http_client(
    base_url: str, *, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=_HEADERS,
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    )
