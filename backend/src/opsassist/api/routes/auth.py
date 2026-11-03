"""Login, logout and the current-user view."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import select

from opsassist.api.deps import ContainerDep, CurrentUser
from opsassist.api.schemas import LoginRequest, MeOut, UserOut, WorkspaceOut
from opsassist.db import global_scope
from opsassist.models import Membership, Role, Workspace, utcnow

router = APIRouter(prefix="/api/v1", tags=["auth"])


@router.post("/auth/login")
async def login(
    body: LoginRequest, request: Request, response: Response, container: ContainerDep
) -> UserOut:
    client_ip = request.client.host if request.client else "unknown"
    user, token, expires_at = await container.auth.login(body.email, body.password, client_ip)
    settings = container.settings
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=int((expires_at - utcnow()).total_seconds()),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )
    return UserOut.model_validate(user)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, container: ContainerDep) -> None:
    token = request.cookies.get(container.settings.cookie_name)
    if token:
        await container.auth.logout(token)
    response.delete_cookie(container.settings.cookie_name, path="/")


@router.get("/me")
async def me(user: CurrentUser, container: ContainerDep) -> MeOut:
    async with global_scope(container.session_factory) as session:
        rows = await session.execute(
            select(Workspace, Membership.role)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user.id)
            .order_by(Workspace.name)
        )
        workspaces = [
            WorkspaceOut(
                id=workspace.id,
                organization_id=workspace.organization_id,
                name=workspace.name,
                slug=workspace.slug,
                role=Role(role),
            )
            for workspace, role in rows
        ]
    return MeOut(user=UserOut.model_validate(user), workspaces=workspaces)
