"""Database engine and transaction scopes.

Tenant data is only reachable inside `tenant_scope`, which sets the row-level security
context for that transaction. With no context set, RLS policies return no rows.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from opsassist.config import Settings

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        # Bound values can be document text. Keep them out of error messages and logs.
        hide_parameters=True,
    )


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def global_scope(factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    """Transaction for non-tenant tables: users, sessions, memberships, jobs."""
    async with factory() as session, session.begin():
        yield session


@asynccontextmanager
async def tenant_scope(
    factory: SessionFactory, workspace_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Transaction restricted by RLS to one workspace.

    `set_config(..., true)` is transaction-local, so the setting cannot leak to the next
    user of a pooled connection and it works with transaction-mode poolers.
    """
    async with factory() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.workspace_id', :workspace_id, true)"),
            {"workspace_id": str(workspace_id)},
        )
        yield session
