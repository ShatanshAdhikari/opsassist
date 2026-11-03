"""Tool servers, admin review, invocation and the invocation log."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from opsassist.api.deps import WorkspaceContext, WorkspaceDep
from opsassist.api.schemas import RequestBody, ResponseBody
from opsassist.errors import ValidationFailed
from opsassist.identity.permissions import Permission, has_permission
from opsassist.models import ToolDefinition, ToolInvocation, ToolServer
from opsassist.tools import registry
from opsassist.tools.base import ServerKind, ToolAccess
from opsassist.tools.gateway import ToolActor
from opsassist.tools.url_policy import BlockedUrl, check_server_url

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["tools"])

_SLUG = r"^[a-z0-9][a-z0-9\-]{0,63}$"


class RegisterServerRequest(RequestBody):
    slug: str = Field(pattern=_SLUG)
    name: str = Field(min_length=1, max_length=200)
    kind: ServerKind
    url: str | None = Field(default=None, max_length=2000)
    stdio_name: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _target_matches_kind(self) -> RegisterServerRequest:
        if self.kind is ServerKind.GITHUB:
            raise ValueError("GitHub servers are created through /connections/github")
        needs_url = self.kind is ServerKind.MCP_HTTP
        if needs_url != (self.url is not None) or needs_url == (self.stdio_name is not None):
            raise ValueError("mcp_http takes url; mcp_stdio takes stdio_name")
        return self


class UpdateServerRequest(RequestBody):
    enabled: bool


class ReviewToolRequest(RequestBody):
    definition_hash: str = Field(min_length=64, max_length=64)
    risk_level: int = Field(ge=0, le=3)
    access: ToolAccess
    required_scopes: list[str] = Field(default_factory=list, max_length=50)
    timeout_seconds: float | None = Field(default=None, gt=0)
    enabled: bool


class InvokeToolRequest(RequestBody):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ServerOut(ResponseBody):
    id: uuid.UUID
    slug: str
    name: str
    kind: str
    url: str | None
    stdio_name: str | None
    enabled: bool
    health: str
    last_error_code: str | None
    last_checked_at: datetime | None
    last_discovered_at: datetime | None


class DiscoveryOut(BaseModel):
    added: list[str]
    changed: list[str]
    removed: list[str]
    unchanged: list[str]
    rejected: list[dict[str, str]]


class ToolOut(ResponseBody):
    """What members see: only reviewed text, for tools they can call."""

    id: uuid.UUID
    server_id: uuid.UUID
    tool_id: str
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: int
    access: str


class ToolAdminOut(ToolOut):
    """Provider text in here is unreviewed. Render it as plain text."""

    annotations: dict[str, Any]
    definition_hash: str
    review_status: str
    required_scopes: list[str]
    timeout_seconds: float | None
    enabled: bool
    available: bool
    pending_definition: dict[str, Any] | None
    pending_hash: str | None
    reviewed_at: datetime | None
    last_seen_at: datetime


class InvocationOut(BaseModel):
    invocation_id: uuid.UUID
    tool_id: str
    status: str
    is_error: bool
    # Whatever the external system returned: data, never instructions.
    untrusted: Literal[True] = True
    text: str
    structured: dict[str, Any] | None
    truncated: bool
    omitted_blocks: int
    fetched_at: datetime
    latency_ms: float
    attempts: int


class InvocationLogOut(ResponseBody):
    id: uuid.UUID
    actor_user_id: uuid.UUID
    tool_definition_id: uuid.UUID
    tool_id: str
    definition_hash: str
    status: str
    error_code: str | None
    attempts: int
    latency_ms: float | None
    output_chars: int | None
    truncated: bool
    created_at: datetime
    finished_at: datetime | None


def _admin(ctx: WorkspaceContext) -> registry.AdminActor:
    ctx.require(Permission.TOOLS_ADMIN)
    return registry.AdminActor(ctx.workspace_id, ctx.user.id)


@router.post("/tool-servers", status_code=status.HTTP_201_CREATED)
async def register_server(body: RegisterServerRequest, ctx: WorkspaceDep) -> ServerOut:
    actor = _admin(ctx)
    settings = ctx.container.settings
    if body.url is not None:
        hosts = frozenset(host.lower() for host in settings.tool_allowed_private_hosts)
        try:
            await check_server_url(body.url, allowed_private_hosts=hosts)
        except BlockedUrl as exc:
            raise ValidationFailed(str(exc)) from exc
    async with ctx.tx() as session:
        server = await registry.register_server(
            session,
            actor,
            settings,
            slug=body.slug,
            name=body.name,
            kind=body.kind,
            url=body.url,
            stdio_name=body.stdio_name,
        )
    return ServerOut.model_validate(server)


@router.get("/tool-servers")
async def list_servers(ctx: WorkspaceDep) -> list[ServerOut]:
    ctx.require(Permission.TOOLS_ADMIN)
    async with ctx.tx() as session:
        servers = await session.scalars(select(ToolServer).order_by(ToolServer.slug))
        return [ServerOut.model_validate(server) for server in servers]


@router.patch("/tool-servers/{server_id}")
async def update_server(
    server_id: uuid.UUID, body: UpdateServerRequest, ctx: WorkspaceDep
) -> ServerOut:
    actor = _admin(ctx)
    async with ctx.tx() as session:
        server = await registry.set_server_enabled(session, actor, server_id, body.enabled)
    return ServerOut.model_validate(server)


@router.delete("/tool-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_server(server_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    actor = _admin(ctx)
    async with ctx.tx() as session:
        await registry.delete_server(session, actor, server_id)


@router.post("/tool-servers/{server_id}/discover")
async def discover(server_id: uuid.UUID, ctx: WorkspaceDep) -> DiscoveryOut:
    actor = _admin(ctx)
    report = await ctx.container.tools.discover(actor, server_id)
    return DiscoveryOut(
        added=report.added,
        changed=report.changed,
        removed=report.removed,
        unchanged=report.unchanged,
        rejected=report.rejected,
    )


@router.get("/tools")
async def list_tools(ctx: WorkspaceDep) -> list[ToolAdminOut] | list[ToolOut]:
    ctx.require(Permission.WORKSPACE_READ)
    is_admin = has_permission(ctx.role, Permission.TOOLS_ADMIN)
    async with ctx.tx() as session:
        rows = await session.execute(
            select(ToolDefinition, ToolServer)
            .join(ToolServer, ToolServer.id == ToolDefinition.server_id)
            .order_by(ToolDefinition.tool_id)
        )
        pairs = list(rows)
    if is_admin:
        return [ToolAdminOut.model_validate(tool) for tool, _server in pairs]
    if not has_permission(ctx.role, Permission.TOOLS_INVOKE):
        return []
    # Members never see unreviewed provider text.
    return [
        ToolOut.model_validate(tool)
        for tool, server in pairs
        if registry.not_callable_reason(tool, server) is None
    ]


@router.post("/tools/{tool_definition_id}/review")
async def review_tool(
    tool_definition_id: uuid.UUID, body: ReviewToolRequest, ctx: WorkspaceDep
) -> ToolAdminOut:
    actor = _admin(ctx)
    async with ctx.tx() as session:
        tool = await registry.review_tool(
            session,
            actor,
            tool_definition_id,
            definition_hash=body.definition_hash,
            risk_level=body.risk_level,
            access=body.access,
            required_scopes=body.required_scopes,
            timeout_seconds=body.timeout_seconds,
            enabled=body.enabled,
        )
    return ToolAdminOut.model_validate(tool)


@router.post("/tools/{tool_definition_id}/disable")
async def disable_tool(tool_definition_id: uuid.UUID, ctx: WorkspaceDep) -> ToolAdminOut:
    actor = _admin(ctx)
    async with ctx.tx() as session:
        tool = await registry.disable_tool(session, actor, tool_definition_id)
    return ToolAdminOut.model_validate(tool)


@router.post("/tools/{tool_definition_id}/invoke")
async def invoke_tool(
    tool_definition_id: uuid.UUID, body: InvokeToolRequest, ctx: WorkspaceDep
) -> InvocationOut:
    actor = ToolActor(ctx.workspace_id, ctx.user.id, ctx.role)
    result = await ctx.container.tools.invoke(actor, tool_definition_id, body.arguments)
    return InvocationOut(
        invocation_id=result.invocation_id,
        tool_id=result.tool_id,
        status=result.status,
        is_error=result.output.is_error,
        text=result.output.text,
        structured=result.output.structured,
        truncated=result.output.truncated,
        omitted_blocks=result.output.omitted_blocks,
        fetched_at=result.fetched_at,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
    )


@router.get("/tool-invocations")
async def list_invocations(
    ctx: WorkspaceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[InvocationLogOut]:
    ctx.require(Permission.AUDIT_READ)
    async with ctx.tx() as session:
        rows = await session.scalars(
            select(ToolInvocation).order_by(ToolInvocation.created_at.desc()).limit(limit)
        )
        return [InvocationLogOut.model_validate(row) for row in rows]
