"""Queue operations. Enqueue joins the caller's transaction, so jobs and data commit together.

Delivery is at-least-once: a crashed worker's job is reclaimed when its lease expires, so handlers
must be idempotent.
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.db import SessionFactory, global_scope
from opsassist.models import Job, JobStatus, utcnow

_CLAIM_SQL = text(
    """
    UPDATE jobs SET
        status = 'running',
        attempts = attempts + 1,
        locked_by = :worker_id,
        lease_expires_at = now() + make_interval(secs => :lease_seconds)
    WHERE id = (
        SELECT id FROM jobs
        WHERE (status = 'queued' AND run_after <= now())
           OR (status = 'running' AND lease_expires_at < now())
        ORDER BY run_after
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id
    """
)


def enqueue(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    max_attempts: int,
) -> Job:
    now = utcnow()
    job = Job(
        workspace_id=workspace_id,
        kind=kind,
        payload=payload,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=max_attempts,
        run_after=now,
        created_at=now,
    )
    session.add(job)
    return job


async def claim(factory: SessionFactory, worker_id: str, lease_seconds: int) -> Job | None:
    async with global_scope(factory) as session:
        job_id = await session.scalar(
            _CLAIM_SQL, {"worker_id": worker_id, "lease_seconds": float(lease_seconds)}
        )
        if job_id is None:
            return None
        return await session.get(Job, job_id)


async def extend_lease(
    factory: SessionFactory, job_id: uuid.UUID, worker_id: str, lease_seconds: int
) -> bool:
    """Return False if this worker no longer owns the job."""
    async with global_scope(factory) as session:
        result = await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING)
            .values(lease_expires_at=utcnow() + timedelta(seconds=lease_seconds))
            .returning(Job.id)
        )
        return result.first() is not None


async def complete(factory: SessionFactory, job_id: uuid.UUID, worker_id: str) -> None:
    async with global_scope(factory) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING)
            .values(status=JobStatus.SUCCEEDED, finished_at=utcnow(), lease_expires_at=None)
        )


def backoff_seconds(attempts: int, base: float) -> float:
    """Exponential backoff with jitter, capped at one hour."""
    delay: float = min(base * (2 ** max(attempts - 1, 0)), 3600.0)
    return delay * (0.5 + random.random() / 2)  # noqa: S311 - jitter, not security


async def retry_later(
    factory: SessionFactory, job: Job, worker_id: str, error: str, base_backoff: float
) -> None:
    run_after = utcnow() + timedelta(seconds=backoff_seconds(job.attempts, base_backoff))
    async with global_scope(factory) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.locked_by == worker_id)
            .values(
                status=JobStatus.QUEUED,
                run_after=run_after,
                locked_by=None,
                lease_expires_at=None,
                last_error=error[:2000],
            )
        )


async def mark_dead(factory: SessionFactory, job: Job, worker_id: str, error: str) -> None:
    async with global_scope(factory) as session:
        await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.locked_by == worker_id)
            .values(
                status=JobStatus.DEAD,
                finished_at=utcnow(),
                lease_expires_at=None,
                last_error=error[:2000],
            )
        )
