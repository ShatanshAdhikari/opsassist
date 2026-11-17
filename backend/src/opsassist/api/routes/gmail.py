"""Connect a person's own Gmail. Each route acts only on the caller's own mailbox."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from opsassist.api.deps import ContainerDep, CurrentUser, WorkspaceDep
from opsassist.errors import NotFound, ToolUnavailable
from opsassist.identity.permissions import Permission
from opsassist.integrations import gmail_connections as service
from opsassist.integrations.gmail_client import AuthorizationFailed
from opsassist.integrations.gmail_provider import GmailToolProvider
from opsassist.models import utcnow
from opsassist.observability import get_logger
from opsassist.tools.base import ServerHealth, ToolProviderError

router = APIRouter(tags=["gmail"])
log = get_logger("opsassist.gmail")

_WORKSPACE = "/api/v1/workspaces/{workspace_id}/connections/gmail"
_FIELD_CHARS = 2048


class GmailStatusOut(BaseModel):
    configured: bool
    connected: bool
    connection_id: uuid.UUID | None = None
    address: str | None = None
    health: str | None = None
    last_error_code: str | None = None
    last_verified_at: datetime | None = None


class AuthorizeOut(BaseModel):
    authorization_url: str


class VerifyOut(BaseModel):
    verified: bool
    code: str | None


class DisconnectOut(BaseModel):
    revoked_at_provider: bool


@router.get(_WORKSPACE)
async def status_of_own(ctx: WorkspaceDep) -> GmailStatusOut:
    ctx.require(Permission.TOOLS_INVOKE)
    deps = ctx.container.gmail
    configured = service.is_configured(deps)
    found = await service.own_connection(deps, ctx.workspace_id, ctx.user.id)
    if found is None:
        return GmailStatusOut(configured=configured, connected=False)
    connection, server = found
    return GmailStatusOut(
        configured=configured,
        connected=True,
        connection_id=connection.id,
        address=connection.config.get("address"),
        health=server.health,
        last_error_code=server.last_error_code,
        last_verified_at=connection.last_verified_at,
    )


@router.post(f"{_WORKSPACE}/authorize")
async def authorize(ctx: WorkspaceDep) -> AuthorizeOut:
    ctx.require(Permission.TOOLS_INVOKE)
    url = await service.start_authorization(
        ctx.container.gmail, user_id=ctx.user.id, workspace_id=ctx.workspace_id
    )
    return AuthorizeOut(authorization_url=url)


@router.get("/api/v1/oauth/google/callback")
async def callback(
    user: CurrentUser,
    container: ContainerDep,
    state: Annotated[str, Query(max_length=_FIELD_CHARS)] = "",
    code: Annotated[str, Query(max_length=_FIELD_CHARS)] = "",
    error: Annotated[str, Query(max_length=_FIELD_CHARS)] = "",
) -> RedirectResponse:
    """Google sends the browser here. The result returns as a fixed code, never request text."""
    result = "connected"
    if error or not code or not state:
        result = "denied"
    else:
        try:
            await service.complete_authorization(
                container.gmail, state=state, code=code, user_id=user.id
            )
        except AuthorizationFailed as exc:
            log.warning("gmail_authorization_failed", code=exc.code)
            result = exc.code
    query = urlencode({"screen": "integrations", "gmail": result})
    return RedirectResponse(f"/?{query}", status_code=status.HTTP_303_SEE_OTHER)


@router.post(f"{_WORKSPACE}/verify")
async def verify_own(ctx: WorkspaceDep) -> VerifyOut:
    ctx.require(Permission.TOOLS_INVOKE)
    container = ctx.container
    found = await service.own_connection(container.gmail, ctx.workspace_id, ctx.user.id)
    if found is None:
        raise NotFound("Gmail is not connected")
    connection, server = found
    provider = GmailToolProvider(container.gmail, ctx.workspace_id, connection.id)
    code: str | None = None
    try:
        await provider.verify(timeout_seconds=container.settings.tool_default_timeout_seconds)
    except ToolProviderError as exc:
        code = exc.code
    if code in {"server_unavailable", "timeout", "rate_limited"}:
        raise ToolUnavailable(
            "Gmail could not be reached, so nothing was verified", details={"code": code}
        )
    async with ctx.tx() as session:
        stored_server = await session.get(type(server), server.id)
        stored_connection = await session.get(type(connection), connection.id)
        if stored_server is None or stored_connection is None:
            raise NotFound("Gmail is not connected")
        now = utcnow()
        stored_server.last_checked_at = now
        stored_server.last_error_code = code
        stored_server.health = ServerHealth.UNAVAILABLE if code else ServerHealth.HEALTHY
        if code is None:
            stored_connection.last_verified_at = now
    return VerifyOut(verified=code is None, code=code)


@router.delete(_WORKSPACE)
async def disconnect_own(ctx: WorkspaceDep, force: bool = False) -> DisconnectOut:
    ctx.require(Permission.TOOLS_INVOKE)
    deps = ctx.container.gmail
    found = await service.own_connection(deps, ctx.workspace_id, ctx.user.id)
    if found is None:
        raise NotFound("Gmail is not connected")
    revoked = await service.disconnect(
        deps,
        workspace_id=ctx.workspace_id,
        actor_user_id=ctx.user.id,
        connection_id=found[0].id,
        force=force,
    )
    return DisconnectOut(revoked_at_provider=revoked)
