"""Worker loop: claim, run with a renewed lease, then complete, retry or dead-letter."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from typing import Protocol

import structlog

from opsassist.config import Settings
from opsassist.db import SessionFactory
from opsassist.jobs import queue
from opsassist.models import Job
from opsassist.observability import get_logger, redact_secrets

log = get_logger("opsassist.worker")


_RETRIES_EXHAUSTED = "Processing failed after repeated attempts. Upload the file again to retry."


class PermanentJobError(Exception):
    """Retrying cannot succeed. The job goes straight to the dead state.

    The message is shown to users, so it must describe the input and not internals.
    """


class JobHandler(Protocol):
    async def run(self, job: Job) -> None: ...

    async def on_dead(self, job: Job, public_message: str) -> None: ...


class Worker:
    def __init__(
        self,
        settings: Settings,
        factory: SessionFactory,
        handlers: dict[str, JobHandler],
        worker_id: str | None = None,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    async def run_forever(self, stop: asyncio.Event) -> None:
        log.info("worker_started", worker_id=self.worker_id)
        while not stop.is_set():
            try:
                worked = await self.run_once()
            except Exception as exc:
                # A database outage must not kill the worker. It retries after the poll delay.
                log.error("worker_loop_error", error_type=type(exc).__name__)
                worked = False
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._settings.worker_poll_seconds)
        log.info("worker_stopped", worker_id=self.worker_id)

    async def run_once(self) -> bool:
        """Claim and process one job. Return False if none was available."""
        settings = self._settings
        job = await queue.claim(self._factory, self.worker_id, settings.worker_lease_seconds)
        if job is None:
            return False
        structlog.contextvars.bind_contextvars(job_id=str(job.id), job_kind=job.kind)
        try:
            await self._process(job)
        finally:
            structlog.contextvars.unbind_contextvars("job_id", "job_kind")
        return True

    async def _process(self, job: Job) -> None:
        settings = self._settings
        handler = self._handlers.get(job.kind)
        if handler is None:
            await queue.mark_dead(
                self._factory, job, self.worker_id, f"No handler for kind {job.kind}"
            )
            log.error("job_no_handler")
            return
        if job.attempts > job.max_attempts:
            # Reclaimed after repeated crashes: the worker died each time before recording it.
            await self._dead(handler, job, "Worker lost the job repeatedly", _RETRIES_EXHAUSTED)
            return

        renewal = asyncio.create_task(self._renew_lease(job))
        try:
            await handler.run(job)
        except PermanentJobError as exc:
            await self._dead(handler, job, str(exc), str(exc))
        except Exception as exc:
            error = redact_secrets(f"{type(exc).__name__}: {exc}")
            if job.attempts >= job.max_attempts:
                await self._dead(handler, job, error, _RETRIES_EXHAUSTED)
            else:
                log.warning("job_retry", attempts=job.attempts, error_type=type(exc).__name__)
                await queue.retry_later(
                    self._factory, job, self.worker_id, error, settings.job_backoff_base_seconds
                )
        else:
            await queue.complete(self._factory, job.id, self.worker_id)
            log.info("job_succeeded", attempts=job.attempts)
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal

    async def _dead(self, handler: JobHandler, job: Job, error: str, public_message: str) -> None:
        log.error("job_dead", attempts=job.attempts)
        await queue.mark_dead(self._factory, job, self.worker_id, error)
        await handler.on_dead(job, public_message)

    async def _renew_lease(self, job: Job) -> None:
        lease = self._settings.worker_lease_seconds
        while True:
            await asyncio.sleep(lease / 3)
            try:
                await queue.extend_lease(self._factory, job.id, self.worker_id, lease)
            except Exception as exc:
                log.warning("lease_renewal_failed", error_type=type(exc).__name__)
