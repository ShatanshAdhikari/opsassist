"""The one place that decides which sources a user may read.

Every query that returns document content must use `VISIBLE_SOURCES_SQL`. Workspace isolation is
also enforced by RLS. Grants are checked on every query with no caching, so a revocation takes
effect on the next request. Workspace role grants nothing here: admins need explicit grants to
read restricted sources.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

VISIBLE_SOURCES_SQL = """
    SELECT s.id
    FROM knowledge_sources s
    WHERE s.workspace_id = :workspace_id
      AND (
        s.visibility = 'workspace'
        OR EXISTS (
            SELECT 1 FROM source_grants g
            WHERE g.source_id = s.id AND g.user_id = :user_id
        )
        OR EXISTS (
            SELECT 1 FROM source_grants g
            JOIN group_members m ON m.group_id = g.group_id
            WHERE g.source_id = s.id AND m.user_id = :user_id
        )
      )
"""


async def visible_source_ids(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> set[uuid.UUID]:
    rows = await session.execute(
        text(VISIBLE_SOURCES_SQL), {"workspace_id": workspace_id, "user_id": user_id}
    )
    return {row[0] for row in rows}


async def can_read_source(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, source_id: uuid.UUID
) -> bool:
    return source_id in await visible_source_ids(session, workspace_id, user_id)
