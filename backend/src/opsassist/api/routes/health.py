"""Liveness and readiness. Unauthenticated: reveal no configuration beyond up or down."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from opsassist.api.deps import ContainerDep
from opsassist.observability import get_logger

router = APIRouter(tags=["health"])
log = get_logger("opsassist.health")


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(container: ContainerDep) -> JSONResponse:
    try:
        async with container.session_factory() as session:
            # alembic_version is not granted to the app role, so check a migrated table instead.
            await session.execute(text("SELECT 1 FROM organizations LIMIT 1"))
    except (SQLAlchemyError, OSError) as exc:
        log.warning("readiness_failed", error_type=type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ready"})
