"""Explicit role-to-permission map. Restricted content access is separate: see knowledge.access."""

from __future__ import annotations

from enum import StrEnum

from opsassist.errors import PermissionDenied
from opsassist.models import Role


class Permission(StrEnum):
    WORKSPACE_READ = "workspace:read"
    KNOWLEDGE_QUERY = "knowledge:query"
    DOCUMENTS_WRITE = "documents:write"
    SOURCES_CREATE = "sources:create"
    SOURCES_ADMIN = "sources:admin"
    MEMBERS_MANAGE = "members:manage"
    AUDIT_READ = "audit:read"
    TOOLS_INVOKE = "tools:invoke"
    TOOLS_ADMIN = "tools:admin"


_VIEWER = frozenset({Permission.WORKSPACE_READ, Permission.KNOWLEDGE_QUERY})
_MEMBER = _VIEWER | {
    Permission.DOCUMENTS_WRITE,
    Permission.SOURCES_CREATE,
    Permission.TOOLS_INVOKE,
}
_ADMIN = _MEMBER | {
    Permission.SOURCES_ADMIN,
    Permission.MEMBERS_MANAGE,
    Permission.AUDIT_READ,
    Permission.TOOLS_ADMIN,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.MEMBER: _MEMBER,
    Role.ADMIN: _ADMIN,
    Role.OWNER: _ADMIN,
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def require_permission(role: Role, permission: Permission) -> None:
    if not has_permission(role, permission):
        raise PermissionDenied(f"Role '{role}' lacks permission '{permission}'")
