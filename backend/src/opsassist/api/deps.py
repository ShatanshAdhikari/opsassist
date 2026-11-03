"""Request dependencies: authentication, workspace resolution, transaction scopes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.container import Container
from opsassist.db import global_scope, tenant_scope
from opsassist.errors import AuthenticationRequired, NotFound
from opsassist.identity.permissions import Permission, require_permission
from opsassist.models import Membership, Role, User


def get_container(request: Request) -> Container:
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]


async def current_user(request: Request, container: ContainerDep) -> User:
    token = request.cookies.get(container.settings.cookie_name)
    if not token:
        raise AuthenticationRequired("Not authenticated")
    return await container.auth.authenticate(token)


CurrentUser = Annotated[User, Depends(current_user)]


@dataclass(frozen=True)
class WorkspaceContext:
    """An authenticated user acting in one workspace."""

    container: Container
    user: User
    workspace_id: uuid.UUID
    role: Role

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[AsyncSession]:
        async with tenant_scope(self.container.session_factory, self.workspace_id) as session:
            yield session

    def require(self, permission: Permission) -> None:
        require_permission(self.role, permission)


async def workspace_context(
    workspace_id: uuid.UUID, user: CurrentUser, container: ContainerDep
) -> WorkspaceContext:
    async with global_scope(container.session_factory) as session:
        membership = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == workspace_id, Membership.user_id == user.id
            )
        )
    if membership is None:
        # Same response as a workspace that does not exist.
        raise NotFound("Workspace not found")
    return WorkspaceContext(container, user, workspace_id, Role(membership.role))


WorkspaceDep = Annotated[WorkspaceContext, Depends(workspace_context)]
