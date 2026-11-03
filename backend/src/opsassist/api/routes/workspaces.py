"""Workspace members, groups and audit history."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import select

from opsassist.api.deps import WorkspaceDep
from opsassist.api.schemas import (
    AddGroupMemberRequest,
    AddMemberRequest,
    AuditEventOut,
    CreateGroupRequest,
    GroupOut,
    MemberOut,
    UserOut,
)
from opsassist.identity import workspaces as service
from opsassist.identity.permissions import Permission
from opsassist.models import AuditEvent, Role

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["workspaces"])


@router.get("/members")
async def list_members(ctx: WorkspaceDep) -> list[MemberOut]:
    ctx.require(Permission.WORKSPACE_READ)
    async with ctx.tx() as session:
        rows = await service.list_members(session, ctx.workspace_id)
    return [MemberOut(user=UserOut.model_validate(u), role=Role(m.role)) for m, u in rows]


@router.post("/members", status_code=status.HTTP_201_CREATED)
async def add_member(body: AddMemberRequest, ctx: WorkspaceDep) -> MemberOut:
    ctx.require(Permission.MEMBERS_MANAGE)
    async with ctx.tx() as session:
        membership, user = await service.add_member(
            session, ctx.workspace_id, ctx.user.id, ctx.role, body.email, body.role
        )
    return MemberOut(user=UserOut.model_validate(user), role=Role(membership.role))


@router.delete("/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    ctx.require(Permission.MEMBERS_MANAGE)
    async with ctx.tx() as session:
        await service.remove_member(session, ctx.workspace_id, ctx.user.id, ctx.role, user_id)


@router.get("/groups")
async def list_groups(ctx: WorkspaceDep) -> list[GroupOut]:
    ctx.require(Permission.WORKSPACE_READ)
    async with ctx.tx() as session:
        groups = await service.list_groups(session, ctx.workspace_id)
    return [GroupOut.model_validate(group) for group in groups]


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def create_group(body: CreateGroupRequest, ctx: WorkspaceDep) -> GroupOut:
    ctx.require(Permission.MEMBERS_MANAGE)
    async with ctx.tx() as session:
        group = await service.create_group(session, ctx.workspace_id, ctx.user.id, body.name)
    return GroupOut.model_validate(group)


@router.post("/groups/{group_id}/members", status_code=status.HTTP_204_NO_CONTENT)
async def add_group_member(
    group_id: uuid.UUID, body: AddGroupMemberRequest, ctx: WorkspaceDep
) -> None:
    ctx.require(Permission.MEMBERS_MANAGE)
    async with ctx.tx() as session:
        await service.add_group_member(
            session, ctx.workspace_id, ctx.user.id, group_id, body.user_id
        )


@router.delete("/groups/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member(group_id: uuid.UUID, user_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    ctx.require(Permission.MEMBERS_MANAGE)
    async with ctx.tx() as session:
        await service.remove_group_member(session, ctx.workspace_id, ctx.user.id, group_id, user_id)


@router.get("/audit")
async def list_audit(
    ctx: WorkspaceDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[AuditEventOut]:
    ctx.require(Permission.AUDIT_READ)
    async with ctx.tx() as session:
        events = await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.workspace_id == ctx.workspace_id)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(limit)
        )
        return [
            AuditEventOut(
                id=event.id,
                occurred_at=event.occurred_at,
                actor_user_id=event.actor_user_id,
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                outcome=event.outcome,
                request_id=event.request_id,
                metadata=event.event_metadata,
            )
            for event in events
        ]
