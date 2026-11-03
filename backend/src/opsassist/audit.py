"""Append-only audit trail. The event commits in the caller's transaction."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.models import AuditEvent


def record_event(
    session: AsyncSession,
    action: str,
    *,
    workspace_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: object | None = None,
    outcome: str = "success",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Add an audit event. Metadata holds identifiers and counts, never document content."""
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    session.add(
        AuditEvent(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            outcome=outcome,
            request_id=request_id,
            event_metadata=metadata or {},
        )
    )
