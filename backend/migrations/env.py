"""Alembic environment. Uses the owner role: MIGRATION_DATABASE_URL, never the app role."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from opsassist.models import Base

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _dotenv_value(key: str) -> str | None:
    """Read one key from a .env file so migrations work with the documented setup."""
    for location in (Path(".env"), Path("../.env")):
        if not location.exists():
            continue
        for line in location.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


def _database_url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL") or _dotenv_value("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required to run migrations")
    return url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")

asyncio.run(_run_online())
