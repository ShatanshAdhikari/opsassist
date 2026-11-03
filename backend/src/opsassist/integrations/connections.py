"""Connection lifecycle: create, re-key, change scope, verify, revoke."""

from __future__ import annotations

import re
import uuid
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.audit import record_event
from opsassist.credentials.store import CredentialStore
from opsassist.errors import Conflict, NotFound, ValidationFailed
from opsassist.integrations.github_client import InstallationTokens
from opsassist.models import IntegrationConnection, ToolServer, utcnow
from opsassist.tools.base import ServerHealth, ServerKind
from opsassist.tools.registry import AdminActor

GITHUB = "github"
_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,99}/[A-Za-z0-9_.\-]{1,100}$")


def _check_private_key(private_key: str) -> None:
    """GitHub App keys are RSA. Errors never quote the key."""
    try:
        key = load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError):
        # `from None`: the underlying error could describe the key's contents.
        raise ValidationFailed("The private key is not an unencrypted PEM key") from None
    if not isinstance(key, RSAPrivateKey):
        raise ValidationFailed("GitHub App private keys are RSA keys")


def _check_repositories(repositories: list[str]) -> list[str]:
    """One owner only: an installation token can only span a single account."""
    if not repositories:
        raise ValidationFailed("List at least one repository")
    unique = sorted({name.strip() for name in repositories}, key=str.lower)
    for name in unique:
        if not _REPOSITORY.fullmatch(name) or name.endswith(("/.", "/..")):
            raise ValidationFailed("Repositories must be written as owner/name")
    if len({name.split("/", 1)[0].lower() for name in unique}) != 1:
        raise ValidationFailed("All repositories in a connection must have the same owner")
    return unique


async def create_github_connection(
    session: AsyncSession,
    actor: AdminActor,
    store: CredentialStore,
    *,
    slug: str,
    name: str,
    app_id: str,
    installation_id: int,
    private_key: str,
    repositories: list[str],
) -> tuple[IntegrationConnection, ToolServer]:
    store.require_configured()
    _check_private_key(private_key)
    allowed = _check_repositories(repositories)
    if await session.scalar(select(ToolServer.id).where(ToolServer.slug == slug)):
        raise Conflict("A tool server with this slug already exists")
    if await session.scalar(
        select(IntegrationConnection.id).where(IntegrationConnection.name == name)
    ):
        raise Conflict("A connection with this name already exists")
    credential_id = await store.put(session, actor.workspace_id, {"private_key": private_key})
    connection = IntegrationConnection(
        workspace_id=actor.workspace_id,
        provider=GITHUB,
        name=name,
        config={"app_id": app_id, "installation_id": installation_id, "repositories": allowed},
        credential_id=credential_id,
        created_by=actor.user_id,
    )
    session.add(connection)
    await session.flush()
    server = ToolServer(
        workspace_id=actor.workspace_id,
        slug=slug,
        name=name,
        kind=ServerKind.GITHUB,
        connection_id=connection.id,
        enabled=True,
        health=ServerHealth.UNKNOWN,
        created_by=actor.user_id,
    )
    session.add(server)
    await session.flush()
    record_event(
        session,
        "connection.create",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="connection",
        target_id=connection.id,
        metadata={"provider": GITHUB, "repositories": allowed},
    )
    return connection, server


async def get_connection(session: AsyncSession, connection_id: uuid.UUID) -> IntegrationConnection:
    connection = await session.get(IntegrationConnection, connection_id)
    if connection is None:
        raise NotFound("Connection not found")
    return connection


async def server_for(session: AsyncSession, connection_id: uuid.UUID) -> ToolServer:
    server = await session.scalar(
        select(ToolServer).where(ToolServer.connection_id == connection_id)
    )
    if server is None:
        raise NotFound("Connection not found")
    return server


async def replace_key(
    session: AsyncSession,
    actor: AdminActor,
    store: CredentialStore,
    tokens: InstallationTokens,
    connection_id: uuid.UUID,
    private_key: str,
) -> IntegrationConnection:
    store.require_configured()
    _check_private_key(private_key)
    connection = await get_connection(session, connection_id)
    previous = connection.credential_id
    connection.credential_id = await store.put(
        session, actor.workspace_id, {"private_key": private_key}
    )
    connection.last_verified_at = None
    if previous is not None:
        await store.delete(session, previous)
    tokens.forget_connection(connection.id)
    record_event(
        session,
        "connection.replace_credential",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="connection",
        target_id=connection.id,
    )
    return connection


async def set_repositories(
    session: AsyncSession,
    actor: AdminActor,
    tokens: InstallationTokens,
    connection_id: uuid.UUID,
    repositories: list[str],
) -> IntegrationConnection:
    connection = await get_connection(session, connection_id)
    allowed = _check_repositories(repositories)
    connection.config = {**connection.config, "repositories": allowed}
    connection.last_verified_at = None
    # A cached token still spans the old list. It must not survive a narrowing.
    tokens.forget_connection(connection.id)
    record_event(
        session,
        "connection.set_repositories",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="connection",
        target_id=connection.id,
        metadata={"repositories": allowed},
    )
    return connection


async def delete_connection(
    session: AsyncSession,
    actor: AdminActor,
    store: CredentialStore,
    tokens: InstallationTokens,
    connection_id: uuid.UUID,
) -> None:
    """Destroy our copy of the key and remove the tools.

    This does not revoke anything on GitHub. An admin must also uninstall the app there. A token
    already issued stays valid at GitHub until it expires, but nothing here will use it.
    """
    connection = await get_connection(session, connection_id)
    if connection.credential_id is not None:
        await store.delete(session, connection.credential_id)
    tokens.forget_connection(connection.id)
    record_event(
        session,
        "connection.delete",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="connection",
        target_id=connection.id,
        metadata={"provider": connection.provider, "name": connection.name},
    )
    await session.delete(connection)


def record_verification(
    session: AsyncSession,
    actor: AdminActor,
    connection: IntegrationConnection,
    server: ToolServer,
    *,
    error_code: str | None,
) -> None:
    now = utcnow()
    server.last_checked_at = now
    server.last_error_code = error_code
    server.health = ServerHealth.UNAVAILABLE if error_code else ServerHealth.HEALTHY
    if error_code is None:
        connection.last_verified_at = now
    metadata: dict[str, Any] = {"code": error_code} if error_code else {}
    record_event(
        session,
        "connection.verify",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="connection",
        target_id=connection.id,
        outcome="failure" if error_code else "success",
        metadata=metadata,
    )
