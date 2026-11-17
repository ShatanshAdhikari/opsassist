"""Connections to external systems. No response ever contains a secret."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from opsassist.api.deps import WorkspaceContext, WorkspaceDep
from opsassist.api.routes.tools import ServerOut
from opsassist.api.schemas import RequestBody, ResponseBody
from opsassist.errors import Conflict, ToolUnavailable
from opsassist.identity.permissions import Permission
from opsassist.integrations import connections as service
from opsassist.integrations.github_provider import GitHubToolProvider
from opsassist.integrations.gmail_connections import GMAIL, disconnect
from opsassist.models import IntegrationConnection
from opsassist.tools.base import ToolProviderError
from opsassist.tools.registry import AdminActor

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/connections", tags=["connections"])

_SLUG = r"^[a-z0-9][a-z0-9\-]{0,63}$"
_MAX_KEY_CHARS = 16_000
_REPOSITORIES = Field(min_length=1, max_length=100)


class CreateGitHubConnectionRequest(RequestBody):
    slug: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1, max_length=200)
    app_id: str = Field(pattern=r"^[A-Za-z0-9.\-]{1,64}$")
    installation_id: int = Field(ge=1)
    private_key: SecretStr = Field(max_length=_MAX_KEY_CHARS)
    repositories: list[str] = _REPOSITORIES


class ReplaceKeyRequest(RequestBody):
    private_key: SecretStr = Field(max_length=_MAX_KEY_CHARS)


class SetRepositoriesRequest(RequestBody):
    repositories: list[str] = _REPOSITORIES


class ConnectionOut(ResponseBody):
    id: uuid.UUID
    provider: str
    name: str
    config: dict[str, Any]
    last_verified_at: datetime | None
    created_at: datetime


class CreatedConnectionOut(BaseModel):
    connection: ConnectionOut
    tool_server: ServerOut


class VerifyOut(BaseModel):
    verified: bool
    code: str | None


def _admin(ctx: WorkspaceContext) -> AdminActor:
    ctx.require(Permission.TOOLS_ADMIN)
    return AdminActor(ctx.workspace_id, ctx.user.id)


async def _require_github(ctx: WorkspaceContext, connection_id: uuid.UUID) -> None:
    """Keys, repository lists and App verification mean nothing for other providers."""
    async with ctx.tx() as session:
        connection = await service.get_connection(session, connection_id)
    if connection.provider != service.GITHUB:
        raise Conflict("This action only applies to GitHub connections")


@router.post("/github", status_code=status.HTTP_201_CREATED)
async def create_github(
    body: CreateGitHubConnectionRequest, ctx: WorkspaceDep
) -> CreatedConnectionOut:
    actor = _admin(ctx)
    async with ctx.tx() as session:
        connection, server = await service.create_github_connection(
            session,
            actor,
            ctx.container.credentials,
            slug=body.slug,
            name=body.name,
            app_id=body.app_id,
            installation_id=body.installation_id,
            private_key=body.private_key.get_secret_value(),
            repositories=body.repositories,
        )
    return CreatedConnectionOut(
        connection=ConnectionOut.model_validate(connection),
        tool_server=ServerOut.model_validate(server),
    )


@router.get("")
async def list_connections(ctx: WorkspaceDep) -> list[ConnectionOut]:
    ctx.require(Permission.TOOLS_ADMIN)
    async with ctx.tx() as session:
        rows = await session.scalars(
            select(IntegrationConnection).order_by(IntegrationConnection.name)
        )
        return [ConnectionOut.model_validate(row) for row in rows]


@router.put("/{connection_id}/key")
async def replace_key(
    connection_id: uuid.UUID, body: ReplaceKeyRequest, ctx: WorkspaceDep
) -> ConnectionOut:
    actor = _admin(ctx)
    container = ctx.container
    await _require_github(ctx, connection_id)
    async with ctx.tx() as session:
        connection = await service.replace_key(
            session,
            actor,
            container.credentials,
            container.github.tokens,
            connection_id,
            body.private_key.get_secret_value(),
        )
    return ConnectionOut.model_validate(connection)


@router.put("/{connection_id}/repositories")
async def set_repositories(
    connection_id: uuid.UUID, body: SetRepositoriesRequest, ctx: WorkspaceDep
) -> ConnectionOut:
    actor = _admin(ctx)
    await _require_github(ctx, connection_id)
    async with ctx.tx() as session:
        connection = await service.set_repositories(
            session, actor, ctx.container.github.tokens, connection_id, body.repositories
        )
    return ConnectionOut.model_validate(connection)


@router.post("/{connection_id}/verify")
async def verify(connection_id: uuid.UUID, ctx: WorkspaceDep) -> VerifyOut:
    """Live check of key, app, installation and repository list. Reports failure in the body."""
    actor = _admin(ctx)
    container = ctx.container
    await _require_github(ctx, connection_id)
    provider = GitHubToolProvider(container.github, ctx.workspace_id, connection_id)
    code: str | None = None
    try:
        await provider.verify(timeout_seconds=container.settings.tool_default_timeout_seconds)
    except ToolProviderError as exc:
        code = exc.code
    async with ctx.tx() as session:
        connection = await service.get_connection(session, connection_id)
        server = await service.server_for(session, connection_id)
        service.record_verification(session, actor, connection, server, error_code=code)
    if code in {"server_unavailable", "timeout"}:
        raise ToolUnavailable(
            "GitHub could not be reached, so nothing was verified", details={"code": code}
        )
    return VerifyOut(verified=code is None, code=code)


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    connection_id: uuid.UUID, ctx: WorkspaceDep, force: bool = False
) -> None:
    """Removes our copy of the key and the tools. Uninstall the app on GitHub as well.

    An admin may remove someone's mailbox connection, which revokes it at Google first.
    """
    actor = _admin(ctx)
    container = ctx.container
    async with ctx.tx() as session:
        provider = (await service.get_connection(session, connection_id)).provider
    if provider == GMAIL:
        await disconnect(
            container.gmail,
            workspace_id=ctx.workspace_id,
            actor_user_id=ctx.user.id,
            connection_id=connection_id,
            force=force,
        )
        return
    async with ctx.tx() as session:
        await service.delete_connection(
            session, actor, container.credentials, container.github.tokens, connection_id
        )
