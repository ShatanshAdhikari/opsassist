"""Workspace membership and group management."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.audit import record_event
from opsassist.errors import Conflict, NotFound, PermissionDenied
from opsassist.models import Group, GroupMember, Membership, Role, User


async def list_members(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[tuple[Membership, User]]:
    rows = await session.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == workspace_id)
        .order_by(User.email)
    )
    return [(row[0], row[1]) for row in rows]


async def add_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_role: Role,
    email: str,
    role: Role,
) -> tuple[Membership, User]:
    if role == Role.OWNER and actor_role != Role.OWNER:
        raise PermissionDenied("Only an owner can add another owner")
    user = await session.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if user is None:
        # Membership management is admin-only, so revealing absence here is acceptable.
        raise NotFound("No user with that email. Create the account first.")
    existing = await session.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user.id
        )
    )
    if existing is not None:
        raise Conflict("User is already a member")
    membership = Membership(workspace_id=workspace_id, user_id=user.id, role=role)
    session.add(membership)
    record_event(
        session,
        "workspace.member_added",
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        target_type="user",
        target_id=user.id,
        metadata={"role": role},
    )
    return membership, user


async def remove_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    actor_role: Role,
    user_id: uuid.UUID,
) -> None:
    membership = await session.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    if membership is None:
        raise NotFound("Member not found")
    if membership.role == Role.OWNER:
        if actor_role != Role.OWNER:
            raise PermissionDenied("Only an owner can remove an owner")
        owners = await session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == workspace_id, Membership.role == Role.OWNER)
        )
        if owners == 1:
            raise Conflict("Cannot remove the last owner")
    # Group membership is workspace-scoped and must not outlive workspace membership.
    for member in await session.scalars(
        select(GroupMember).where(
            GroupMember.workspace_id == workspace_id, GroupMember.user_id == user_id
        )
    ):
        await session.delete(member)
    await session.delete(membership)
    record_event(
        session,
        "workspace.member_removed",
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        target_type="user",
        target_id=user_id,
    )


async def create_group(
    session: AsyncSession, workspace_id: uuid.UUID, actor_id: uuid.UUID, name: str
) -> Group:
    group = Group(workspace_id=workspace_id, name=name)
    session.add(group)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("A group with that name already exists") from exc
    record_event(
        session,
        "workspace.group_created",
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        target_type="group",
        target_id=group.id,
    )
    return group


async def list_groups(session: AsyncSession, workspace_id: uuid.UUID) -> list[Group]:
    result = await session.scalars(
        select(Group).where(Group.workspace_id == workspace_id).order_by(Group.name)
    )
    return list(result)


async def add_group_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    group = await session.scalar(
        select(Group).where(Group.id == group_id, Group.workspace_id == workspace_id)
    )
    if group is None:
        raise NotFound("Group not found")
    member = await session.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    if member is None:
        raise NotFound("User is not a member of this workspace")
    session.add(GroupMember(workspace_id=workspace_id, group_id=group_id, user_id=user_id))
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("User is already in the group") from exc
    record_event(
        session,
        "workspace.group_member_added",
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        target_type="group",
        target_id=group_id,
        metadata={"user_id": str(user_id)},
    )


async def remove_group_member(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    actor_id: uuid.UUID,
    group_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    member = await session.scalar(
        select(GroupMember).where(
            GroupMember.workspace_id == workspace_id,
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if member is None:
        raise NotFound("Group member not found")
    await session.delete(member)
    record_event(
        session,
        "workspace.group_member_removed",
        workspace_id=workspace_id,
        actor_user_id=actor_id,
        target_type="group",
        target_id=group_id,
        metadata={"user_id": str(user_id)},
    )
