"""Connect, reconnect and disconnect one person's mailbox.

A mailbox belongs to whoever signed in to it. Nobody else in the workspace can use it, including
admins: they can only review its tools and remove the connection.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select

from opsassist.audit import record_event
from opsassist.credentials.crypto import CredentialDecryptError
from opsassist.db import global_scope, tenant_scope
from opsassist.errors import (
    Conflict,
    NotFound,
    ServiceUnavailable,
    ToolUnavailable,
)
from opsassist.identity.permissions import Permission, has_permission
from opsassist.integrations.gmail_client import (
    AuthorizationFailed,
    GmailApi,
    GmailConnection,
    authorization_url,
    exchange_code,
    new_http_client,
    new_pkce_pair,
    revoke,
)
from opsassist.integrations.gmail_provider import TOOLS, GmailDeps
from opsassist.models import IntegrationConnection, Membership, OAuthState, Role, ToolServer, utcnow
from opsassist.tools.base import ServerHealth, ServerKind, ToolProviderError
from opsassist.tools.registry import AdminActor, apply_discovery

GMAIL = "gmail"
_STATE_LIFETIME = timedelta(minutes=10)
_OAUTH_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class _PendingSignIn:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    code_verifier: str


def _state_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def is_configured(deps: GmailDeps) -> bool:
    settings = deps.settings
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


async def start_authorization(
    deps: GmailDeps, *, user_id: uuid.UUID, workspace_id: uuid.UUID
) -> str:
    """Return where to send this person's browser. The state ties the callback back to them."""
    if not is_configured(deps):
        raise ServiceUnavailable(
            "Gmail is not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
        )
    deps.credentials.require_configured()
    state = secrets.token_urlsafe(32)
    verifier, challenge = new_pkce_pair()
    now = utcnow()
    async with global_scope(deps.session_factory) as session:
        await session.execute(delete(OAuthState).where(OAuthState.expires_at < now))
        session.add(
            OAuthState(
                state_hash=_state_hash(state),
                user_id=user_id,
                workspace_id=workspace_id,
                provider=GMAIL,
                code_verifier=verifier,
                expires_at=now + _STATE_LIFETIME,
            )
        )
    return authorization_url(deps.settings, state=state, challenge=challenge)


async def _consume_state(deps: GmailDeps, state: str, user_id: uuid.UUID) -> _PendingSignIn:
    """Single use. A state from someone else's browser is refused, which stops login CSRF.

    Checks happen after the transaction, so a refused state is still used up.
    """
    async with global_scope(deps.session_factory) as session:
        # Deleting and reading as one statement means two callbacks cannot both use a state.
        row = (
            await session.execute(
                delete(OAuthState)
                .where(OAuthState.state_hash == _state_hash(state), OAuthState.provider == GMAIL)
                .returning(
                    OAuthState.user_id,
                    OAuthState.workspace_id,
                    OAuthState.code_verifier,
                    OAuthState.expires_at,
                )
            )
        ).first()
        if row is None:
            raise AuthorizationFailed("invalid_state")
        pending = _PendingSignIn(row.user_id, row.workspace_id, row.code_verifier)
        expired = row.expires_at < utcnow()
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == pending.workspace_id, Membership.user_id == user_id
            )
        )
        role = Role(membership.role) if membership is not None else None
    if pending.user_id != user_id or expired:
        raise AuthorizationFailed("invalid_state")
    if role is None or not has_permission(role, Permission.TOOLS_INVOKE):
        raise AuthorizationFailed("not_permitted")
    return pending


async def complete_authorization(
    deps: GmailDeps, *, state: str, code: str, user_id: uuid.UUID
) -> None:
    pending = await _consume_state(deps, state, user_id)
    settings = deps.settings
    async with new_http_client(
        timeout_seconds=_OAUTH_TIMEOUT_SECONDS, transport=deps.transport
    ) as http:
        try:
            granted = await exchange_code(http, settings, code=code, verifier=pending.code_verifier)
        except ToolProviderError as exc:
            raise AuthorizationFailed("google_unavailable") from exc
        try:
            # The address decides what counts as "from me", so read it from Gmail itself.
            probe = GmailConnection(uuid.uuid4(), uuid.uuid4(), "", granted.refresh_token)
            profile = await GmailApi(http, settings, deps.tokens, probe).check_authentication()
            deps.tokens.invalidate(probe)
            address = profile.get("emailAddress")
            if not isinstance(address, str) or not address:
                raise AuthorizationFailed("google_unavailable")
            await _store(deps, pending, address, granted.refresh_token)
        except (AuthorizationFailed, ToolProviderError) as exc:
            # Nothing will use this grant, so do not leave it live at Google.
            await revoke(http, settings, granted.refresh_token)
            if isinstance(exc, AuthorizationFailed):
                raise
            raise AuthorizationFailed("google_unavailable") from exc


async def _store(
    deps: GmailDeps, pending: _PendingSignIn, address: str, refresh_token: str
) -> None:
    workspace_id, user_id = pending.workspace_id, pending.user_id
    actor = AdminActor(workspace_id, user_id)
    async with tenant_scope(deps.session_factory, workspace_id) as session:
        connection = await session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.owner_user_id == user_id,
                IntegrationConnection.provider == GMAIL,
            )
        )
        reconnected = connection is not None
        credential_id = await deps.credentials.put(
            session, workspace_id, {"refresh_token": refresh_token}
        )
        now = utcnow()
        if connection is not None:
            previous = connection.credential_id
            connection.credential_id = credential_id
            connection.config = {**connection.config, "address": address}
            connection.last_verified_at = now
            if previous is not None:
                await deps.credentials.delete(session, previous)
            deps.tokens.forget_connection(connection.id)
            server = await session.scalar(
                select(ToolServer).where(ToolServer.connection_id == connection.id)
            )
            if server is not None:
                server.health = ServerHealth.HEALTHY
                server.last_error_code = None
                server.last_checked_at = now
        else:
            name = f"Gmail ({address})"
            if await session.scalar(
                select(IntegrationConnection.id).where(IntegrationConnection.name == name)
            ):
                raise AuthorizationFailed("already_connected")
            connection = IntegrationConnection(
                workspace_id=workspace_id,
                owner_user_id=user_id,
                provider=GMAIL,
                name=name,
                config={"address": address},
                credential_id=credential_id,
                last_verified_at=now,
                created_by=user_id,
            )
            session.add(connection)
            await session.flush()
            slug = GMAIL
            if await session.scalar(select(ToolServer.id).where(ToolServer.slug == slug)):
                slug = f"{GMAIL}-{user_id.hex[:12]}"
            server = ToolServer(
                workspace_id=workspace_id,
                owner_user_id=user_id,
                slug=slug,
                name=name,
                kind=ServerKind.GMAIL,
                connection_id=connection.id,
                enabled=True,
                health=ServerHealth.HEALTHY,
                last_checked_at=now,
                created_by=user_id,
            )
            session.add(server)
            await session.flush()
            # Definitions are known without a call. They still start unreviewed and disabled.
            await apply_discovery(
                session,
                actor,
                server.id,
                TOOLS.values(),
                max_schema_bytes=deps.settings.tool_max_schema_bytes,
            )
        record_event(
            session,
            "connection.reconnect" if reconnected else "connection.create",
            workspace_id=workspace_id,
            actor_user_id=user_id,
            target_type="connection",
            target_id=connection.id,
            metadata={"provider": GMAIL},
        )


async def own_connection(
    deps: GmailDeps, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[IntegrationConnection, ToolServer] | None:
    async with tenant_scope(deps.session_factory, workspace_id) as session:
        connection = await session.scalar(
            select(IntegrationConnection).where(
                IntegrationConnection.owner_user_id == user_id,
                IntegrationConnection.provider == GMAIL,
            )
        )
        if connection is None:
            return None
        server = await session.scalar(
            select(ToolServer).where(ToolServer.connection_id == connection.id)
        )
        if server is None:
            raise NotFound("Connection not found")
        return connection, server


async def disconnect(
    deps: GmailDeps,
    *,
    workspace_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    connection_id: uuid.UUID,
    force: bool = False,
) -> bool:
    """Revoke at Google, then remove our copy. Returns whether Google confirmed the revocation.

    If Google cannot be reached the connection is kept, so access is not left live with nobody able
    to see it. `force` removes it anyway, for a person who will revoke it in their Google account.
    """
    async with tenant_scope(deps.session_factory, workspace_id) as session:
        connection = await session.get(IntegrationConnection, connection_id)
        if connection is None or connection.provider != GMAIL:
            raise NotFound("Connection not found")
        token: str | None = None
        if connection.credential_id is not None:
            try:
                secret = await deps.credentials.get(session, connection.credential_id)
                token = str(secret["refresh_token"])
            except CredentialDecryptError:
                token = None

    revoked = False
    if token is not None:
        async with new_http_client(
            timeout_seconds=_OAUTH_TIMEOUT_SECONDS, transport=deps.transport
        ) as http:
            revoked = await revoke(http, deps.settings, token)
    if not revoked and not force:
        raise ToolUnavailable(
            "Google could not confirm the revocation, so the connection was kept. Try again, or "
            "remove it anyway and revoke access in your Google account.",
            details={"code": "revocation_unconfirmed"},
        )

    async with tenant_scope(deps.session_factory, workspace_id) as session:
        connection = await session.get(IntegrationConnection, connection_id)
        if connection is None:
            raise Conflict("The connection was already removed")
        if connection.credential_id is not None:
            await deps.credentials.delete(session, connection.credential_id)
        deps.tokens.forget_connection(connection_id)
        record_event(
            session,
            "connection.delete",
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            target_type="connection",
            target_id=connection_id,
            metadata={"provider": GMAIL, "revoked_at_provider": revoked},
        )
        await session.delete(connection)
    return revoked
