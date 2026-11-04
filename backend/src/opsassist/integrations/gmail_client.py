"""Google sign-in and read-only Gmail HTTP access.

Only `gmail.readonly` is ever requested, and a grant of anything else is refused. So a bug above
this layer cannot send, delete or label mail with these tokens. Access tokens stay in memory. The
refresh token goes in the encrypted credential store, and this module only receives it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from opsassist.config import Settings
from opsassist.tools.base import (
    CredentialRejected,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnreachable,
    ReauthorizationRequired,
)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_TOKEN_EARLY_REFRESH_SECONDS = 120
_DEFAULT_RETRY_SECONDS = 60.0
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"}


class GmailNotFound(Exception):
    """The thread does not exist in this mailbox."""


class GmailRejected(Exception):
    """Gmail refused the request itself, for example a query it cannot parse."""


class AuthorizationFailed(Exception):
    """Sign-in could not complete. `code` is fixed text, safe to show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GmailConnection:
    """Settings plus the decrypted refresh token. Lives in memory for one operation."""

    connection_id: Any
    credential_id: Any
    address: str
    refresh_token: str = ""

    def __repr__(self) -> str:
        # Keeps the token out of tracebacks and debug output.
        return f"GmailConnection(connection_id={self.connection_id})"


@dataclass(frozen=True)
class GrantedTokens:
    refresh_token: str
    access_token: str


def new_http_client(
    *, timeout_seconds: float, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": "opsassist"},
        timeout=timeout_seconds,
        follow_redirects=False,
        transport=transport,
    )


def new_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorization_url(settings: Settings, *, state: str, challenge: str) -> str:
    """Offline access for a refresh token. Consent is forced, since Google omits one otherwise."""
    query = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{settings.google_oauth_authorize_url}?{urlencode(query)}"


def _client_credentials(settings: Settings) -> dict[str, str]:
    secret = settings.google_oauth_client_secret
    if not settings.google_oauth_client_id or secret is None:
        raise CredentialRejected()
    return {
        "client_id": settings.google_oauth_client_id,
        "client_secret": secret.get_secret_value(),
    }


def _parse_json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except ValueError as exc:
        raise ProviderProtocolError() from exc


async def _post_form(http: httpx.AsyncClient, url: str, data: dict[str, str]) -> httpx.Response:
    try:
        return await http.post(url, data=data)
    except httpx.TimeoutException as exc:
        raise ProviderTimeout() from exc
    except httpx.TransportError as exc:
        raise ProviderUnreachable() from exc


async def exchange_code(
    http: httpx.AsyncClient, settings: Settings, *, code: str, verifier: str
) -> GrantedTokens:
    response = await _post_form(
        http,
        settings.google_oauth_token_url,
        {
            **_client_credentials(settings),
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": settings.google_oauth_redirect_uri,
        },
    )
    if response.status_code != 200:
        raise AuthorizationFailed("exchange_failed")
    body = _parse_json(response.content)
    refresh_token, access_token = body.get("refresh_token"), body.get("access_token")
    if not access_token:
        raise AuthorizationFailed("exchange_failed")
    if set(str(body.get("scope", "")).split()) != {GMAIL_READONLY_SCOPE}:
        # Narrower cannot work, and broader must not be kept. Give back what was granted.
        await revoke(http, settings, str(refresh_token or access_token))
        raise AuthorizationFailed("wrong_scope")
    if not refresh_token:
        raise AuthorizationFailed("no_refresh_token")
    return GrantedTokens(str(refresh_token), str(access_token))


async def revoke(http: httpx.AsyncClient, settings: Settings, token: str) -> bool:
    """True when Google no longer honours the token, including when it had already expired."""
    try:
        response = await http.post(settings.google_oauth_revoke_url, data={"token": token})
    except httpx.HTTPError:
        return False
    if response.status_code == 200:
        return True
    if response.status_code == 400:
        try:
            return bool(json.loads(response.content).get("error") == "invalid_token")
        except ValueError:
            return False
    return False


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "")
    return float(value) if value.isdigit() else _DEFAULT_RETRY_SECONDS


def _rate_limit(response: httpx.Response, content: bytes) -> ProviderRateLimited | None:
    if response.status_code == 429:
        return ProviderRateLimited(_retry_after(response))
    if response.status_code != 403:
        return None
    try:
        errors = json.loads(content).get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return None
    if any(item.get("reason") in _RATE_LIMIT_REASONS for item in errors):
        return ProviderRateLimited(_retry_after(response))
    return None


class AccessTokens:
    """In-memory cache of short-lived access tokens. They are never persisted."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[Any, Any], tuple[str, float]] = {}

    @staticmethod
    def _key(connection: GmailConnection) -> tuple[Any, Any]:
        # A new sign-in must not reuse a token from the old one.
        return (connection.connection_id, connection.credential_id)

    def invalidate(self, connection: GmailConnection) -> None:
        self._tokens.pop(self._key(connection), None)

    def forget_connection(self, connection_id: Any) -> None:
        for key in [key for key in self._tokens if key[0] == connection_id]:
            del self._tokens[key]

    async def get(
        self, http: httpx.AsyncClient, settings: Settings, connection: GmailConnection
    ) -> str:
        key = self._key(connection)
        cached = self._tokens.get(key)
        if cached is not None and cached[1] > time.time():
            return cached[0]
        token, expires_in = await _refresh(http, settings, connection)
        self._tokens[key] = (token, time.time() + expires_in - _TOKEN_EARLY_REFRESH_SECONDS)
        return token


async def _refresh(
    http: httpx.AsyncClient, settings: Settings, connection: GmailConnection
) -> tuple[str, float]:
    response = await _post_form(
        http,
        settings.google_oauth_token_url,
        {
            **_client_credentials(settings),
            "grant_type": "refresh_token",
            "refresh_token": connection.refresh_token,
        },
    )
    if response.status_code == 200:
        body = _parse_json(response.content)
        scope = body.get("scope")
        if scope is not None and set(str(scope).split()) != {GMAIL_READONLY_SCOPE}:
            raise CredentialRejected()
        try:
            return str(body["access_token"]), float(body["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderProtocolError() from exc
    if rate_limit := _rate_limit(response, response.content):
        raise rate_limit
    if response.status_code >= 500:
        raise ProviderUnreachable()
    try:
        error = json.loads(response.content).get("error")
    except (ValueError, AttributeError):
        error = None
    # Expired, as refresh tokens do after 7 days for a Testing app, or revoked by the person.
    if error == "invalid_grant":
        raise ReauthorizationRequired()
    raise CredentialRejected()


class GmailApi:
    """Authenticated GETs with error mapping and a response size limit."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: Settings,
        tokens: AccessTokens,
        connection: GmailConnection,
    ) -> None:
        self._http = http
        self._settings = settings
        self._tokens = tokens
        self._connection = connection

    async def check_authentication(self) -> dict[str, Any]:
        profile: dict[str, Any] = await self.get("/gmail/v1/users/me/profile")
        return profile

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response, content = await self._send(path, params)
        if response.status_code == 401:
            # Access token expired or revoked early. Try once with a fresh one.
            self._tokens.invalidate(self._connection)
            response, content = await self._send(path, params)
            if response.status_code == 401:
                raise ReauthorizationRequired()
        if response.status_code == 200:
            return _parse_json(content)
        if rate_limit := _rate_limit(response, content):
            raise rate_limit
        if response.status_code == 403:
            raise CredentialRejected()
        if response.status_code == 404:
            raise GmailNotFound()
        if response.status_code == 400:
            raise GmailRejected()
        if response.status_code >= 500:
            raise ProviderUnreachable()
        raise ProviderProtocolError()

    async def _send(self, path: str, params: dict[str, Any] | None) -> tuple[httpx.Response, bytes]:
        token = await self._tokens.get(self._http, self._settings, self._connection)
        url = f"{self._settings.gmail_api_url}{path}"
        try:
            async with self._http.stream(
                "GET", url, params=params, headers={"Authorization": f"Bearer {token}"}
            ) as response:
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > self._settings.gmail_max_response_bytes:
                        raise ProviderProtocolError()
                return response, bytes(content)
        except httpx.TimeoutException as exc:
            raise ProviderTimeout() from exc
        except httpx.TransportError as exc:
            raise ProviderUnreachable() from exc
