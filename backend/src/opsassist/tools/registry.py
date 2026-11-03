"""Tool registry: server registration, discovery, admin review.

A discovered tool starts disabled and classified as the highest risk. It becomes callable only
when an admin reviews the exact definition they were shown, identified by its hash. If the
provider later changes that definition, the tool is disabled and the approved text stays pinned
until someone reviews the new version.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.audit import record_event
from opsassist.config import Settings
from opsassist.errors import Conflict, NotFound, ValidationFailed
from opsassist.models import ToolDefinition, ToolServer, utcnow
from opsassist.tools.base import (
    MAX_RISK_LEVEL,
    READ_ONLY_RISK_LEVEL,
    UNCLASSIFIED_RISK_LEVEL,
    ReviewStatus,
    ServerConfig,
    ServerHealth,
    ServerKind,
    ToolAccess,
    ToolDescriptor,
    hash_definition,
)
from opsassist.tools.schema import InvalidDefinition, check_definition


@dataclass(frozen=True)
class AdminActor:
    workspace_id: uuid.UUID
    user_id: uuid.UUID


@dataclass
class DiscoveryReport:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)


def server_config(server: ToolServer) -> ServerConfig:
    return ServerConfig(
        id=server.id,
        slug=server.slug,
        kind=ServerKind(server.kind),
        url=server.url,
        stdio_name=server.stdio_name,
        workspace_id=server.workspace_id,
        connection_id=server.connection_id,
    )


def not_callable_reason(tool: ToolDefinition, server: ToolServer) -> str | None:
    """Why a tool may not run right now. The one definition of "callable"."""
    if not server.enabled:
        return "server_disabled"
    if tool.review_status != ReviewStatus.APPROVED:
        return "not_reviewed"
    if not tool.enabled:
        return "tool_disabled"
    if not tool.available:
        return "tool_unavailable"
    # Approvals arrive in Phase 4. Until then nothing that can change an external system runs,
    # however an admin has classified it.
    if tool.access != ToolAccess.READ or tool.risk_level != READ_ONLY_RISK_LEVEL:
        return "requires_approval_flow"
    return None


async def get_server(session: AsyncSession, server_id: uuid.UUID) -> ToolServer:
    server = await session.get(ToolServer, server_id)
    if server is None:
        raise NotFound("Tool server not found")
    return server


async def get_tool(
    session: AsyncSession, tool_definition_id: uuid.UUID
) -> tuple[ToolDefinition, ToolServer]:
    tool = await session.get(ToolDefinition, tool_definition_id)
    if tool is None:
        raise NotFound("Tool not found")
    return tool, await get_server(session, tool.server_id)


async def register_server(
    session: AsyncSession,
    actor: AdminActor,
    settings: Settings,
    *,
    slug: str,
    name: str,
    kind: ServerKind,
    url: str | None,
    stdio_name: str | None,
) -> ToolServer:
    if kind is ServerKind.MCP_STDIO and stdio_name not in settings.mcp_stdio_servers:
        raise ValidationFailed("That stdio server is not in the deployment configuration")
    existing = await session.scalar(select(ToolServer.id).where(ToolServer.slug == slug))
    if existing is not None:
        raise Conflict("A tool server with this slug already exists")
    server = ToolServer(
        workspace_id=actor.workspace_id,
        slug=slug,
        name=name,
        kind=kind,
        url=url,
        stdio_name=stdio_name,
        enabled=True,
        health=ServerHealth.UNKNOWN,
        created_by=actor.user_id,
    )
    session.add(server)
    await session.flush()
    record_event(
        session,
        "tool_server.register",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool_server",
        target_id=server.id,
        metadata={"slug": slug, "kind": str(kind)},
    )
    return server


async def set_server_enabled(
    session: AsyncSession, actor: AdminActor, server_id: uuid.UUID, enabled: bool
) -> ToolServer:
    server = await get_server(session, server_id)
    server.enabled = enabled
    record_event(
        session,
        "tool_server.enable" if enabled else "tool_server.disable",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool_server",
        target_id=server.id,
    )
    return server


async def delete_server(session: AsyncSession, actor: AdminActor, server_id: uuid.UUID) -> None:
    server = await get_server(session, server_id)
    record_event(
        session,
        "tool_server.delete",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool_server",
        target_id=server.id,
        metadata={"slug": server.slug},
    )
    await session.delete(server)


def _new_tool(server: ToolServer, descriptor: ToolDescriptor) -> ToolDefinition:
    return ToolDefinition(
        workspace_id=server.workspace_id,
        server_id=server.id,
        name=descriptor.name,
        tool_id=f"{server.slug}.{descriptor.name}",
        description=descriptor.description,
        input_schema=descriptor.input_schema,
        annotations=descriptor.annotations,
        definition_hash=descriptor.definition_hash,
        review_status=ReviewStatus.PENDING,
        risk_level=UNCLASSIFIED_RISK_LEVEL,
        access=ToolAccess.WRITE,
        required_scopes=[],
        enabled=False,
        available=True,
    )


def apply_definition_change(tool: ToolDefinition, descriptor: ToolDescriptor) -> None:
    """Disable a tool whose provider changed it. Approved text stays until a new review."""
    if tool.review_status == ReviewStatus.PENDING:
        # Nothing approved to protect: show the admin the latest version.
        tool.description = descriptor.description
        tool.input_schema = descriptor.input_schema
        tool.annotations = descriptor.annotations
        tool.definition_hash = descriptor.definition_hash
        return
    tool.enabled = False
    tool.review_status = ReviewStatus.CHANGED
    tool.pending_definition = descriptor.as_definition()
    tool.pending_hash = descriptor.definition_hash


async def apply_discovery(
    session: AsyncSession,
    actor: AdminActor,
    server_id: uuid.UUID,
    descriptors: Iterable[ToolDescriptor],
    *,
    max_schema_bytes: int,
) -> DiscoveryReport:
    server = await get_server(session, server_id)
    report = DiscoveryReport()
    valid: dict[str, ToolDescriptor] = {}
    for descriptor in descriptors:
        try:
            check_definition(descriptor, max_schema_bytes=max_schema_bytes)
        except InvalidDefinition as exc:
            report.rejected.append({"name": descriptor.name[:128], "reason": str(exc)})
            continue
        if descriptor.name in valid:
            report.rejected.append({"name": descriptor.name, "reason": "Duplicate tool name"})
            continue
        valid[descriptor.name] = descriptor

    existing = {
        tool.name: tool
        for tool in await session.scalars(
            select(ToolDefinition).where(ToolDefinition.server_id == server.id)
        )
    }
    now = utcnow()
    for name, descriptor in valid.items():
        tool = existing.get(name)
        if tool is None:
            session.add(_new_tool(server, descriptor))
            report.added.append(name)
            continue
        tool.available = True
        tool.last_seen_at = now
        latest = tool.pending_hash or tool.definition_hash
        if descriptor.definition_hash == tool.definition_hash:
            # Provider reverted to the approved version. A tool disabled by the change stays
            # disabled, since a definition that moved once deserves a person looking at it.
            tool.pending_definition = None
            tool.pending_hash = None
            report.unchanged.append(name)
        elif descriptor.definition_hash == latest:
            report.unchanged.append(name)
        else:
            was_approved = tool.review_status == ReviewStatus.APPROVED
            apply_definition_change(tool, descriptor)
            report.changed.append(name)
            if was_approved:
                record_event(
                    session,
                    "tool.definition_changed",
                    workspace_id=actor.workspace_id,
                    actor_user_id=actor.user_id,
                    target_type="tool",
                    target_id=tool.id,
                    outcome="denied",
                    metadata={"tool_id": tool.tool_id, "new_hash": descriptor.definition_hash},
                )
    for name, tool in existing.items():
        if name not in valid and tool.available:
            tool.available = False
            report.removed.append(name)

    server.health = ServerHealth.HEALTHY
    server.last_error_code = None
    server.last_checked_at = now
    server.last_discovered_at = now
    record_event(
        session,
        "tool_server.discover",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool_server",
        target_id=server.id,
        metadata={
            "added": len(report.added),
            "changed": len(report.changed),
            "removed": len(report.removed),
            "rejected": len(report.rejected),
        },
    )
    return report


async def review_tool(
    session: AsyncSession,
    actor: AdminActor,
    tool_definition_id: uuid.UUID,
    *,
    definition_hash: str,
    risk_level: int,
    access: ToolAccess,
    required_scopes: list[str],
    timeout_seconds: float | None,
    enabled: bool,
) -> ToolDefinition:
    """Approve one exact definition. A stale hash means the admin reviewed something else."""
    tool, _server = await get_tool(session, tool_definition_id)
    if not READ_ONLY_RISK_LEVEL <= risk_level <= MAX_RISK_LEVEL:
        raise ValidationFailed("Risk level must be between 0 and 3")
    if access is ToolAccess.WRITE and risk_level == READ_ONLY_RISK_LEVEL:
        raise ValidationFailed("A write tool cannot have risk level 0")
    current_hash = tool.pending_hash or tool.definition_hash
    if definition_hash != current_hash:
        raise Conflict(
            "The tool definition has changed since it was shown. Review the current version.",
            details={"current_hash": current_hash},
        )
    if tool.pending_definition is not None:
        pending: dict[str, Any] = tool.pending_definition
        tool.description = pending["description"]
        tool.input_schema = pending["input_schema"]
        tool.annotations = pending["annotations"]
        tool.definition_hash = hash_definition(pending)
        tool.pending_definition = None
        tool.pending_hash = None
    tool.review_status = ReviewStatus.APPROVED
    tool.risk_level = risk_level
    tool.access = access
    tool.required_scopes = required_scopes
    tool.timeout_seconds = timeout_seconds
    tool.enabled = enabled
    tool.reviewed_by = actor.user_id
    tool.reviewed_at = utcnow()
    record_event(
        session,
        "tool.review",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool",
        target_id=tool.id,
        metadata={
            "tool_id": tool.tool_id,
            "definition_hash": tool.definition_hash,
            "risk_level": risk_level,
            "access": str(access),
            "enabled": enabled,
        },
    )
    return tool


async def disable_tool(
    session: AsyncSession, actor: AdminActor, tool_definition_id: uuid.UUID
) -> ToolDefinition:
    tool, _server = await get_tool(session, tool_definition_id)
    tool.enabled = False
    record_event(
        session,
        "tool.disable",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="tool",
        target_id=tool.id,
        metadata={"tool_id": tool.tool_id},
    )
    return tool
