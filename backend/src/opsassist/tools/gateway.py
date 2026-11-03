"""The only path to a tool call. Every decision here happens outside any model.

Order: role, tenant-scoped lookup, callable check, argument validation, rate limit, circuit
breaker, then the provider. An invocation row is committed before the provider is contacted, so a
crash mid-call leaves a visible `running` record rather than nothing. No database transaction is
held open during the call.

Arguments and output are not stored: only a hash, sizes and the outcome. Output is returned
marked untrusted, because it is whatever the external system said.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update

from opsassist.audit import record_event
from opsassist.config import Settings
from opsassist.db import SessionFactory, tenant_scope
from opsassist.errors import (
    AppError,
    ToolDefinitionChanged,
    ToolNotCallable,
    ToolUnavailable,
)
from opsassist.identity.permissions import Permission, require_permission
from opsassist.identity.service import SlidingWindowLimiter
from opsassist.models import Role, ToolDefinition, ToolInvocation, ToolServer, utcnow
from opsassist.observability import get_logger
from opsassist.tools.base import (
    DefinitionChanged,
    InvocationStatus,
    ServerConfig,
    ServerHealth,
    ToolMissing,
    ToolOutput,
    ToolProviderError,
)
from opsassist.tools.factory import ProviderFactory
from opsassist.tools.registry import (
    AdminActor,
    DiscoveryReport,
    apply_definition_change,
    apply_discovery,
    get_server,
    get_tool,
    not_callable_reason,
    server_config,
)
from opsassist.tools.reliability import CircuitBreaker, call_with_retry
from opsassist.tools.schema import validate_arguments

log = get_logger("opsassist.tools")


@dataclass(frozen=True)
class ToolActor:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: Role


@dataclass(frozen=True)
class InvocationResult:
    invocation_id: uuid.UUID
    tool_id: str
    status: InvocationStatus
    output: ToolOutput
    fetched_at: datetime
    latency_ms: float
    attempts: int


@dataclass(frozen=True)
class _Plan:
    invocation_id: uuid.UUID
    tool_id: str
    server: ServerConfig
    name: str
    definition_hash: str
    timeout_seconds: float


def _arguments_hash(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ToolGateway:
    def __init__(
        self,
        settings: Settings,
        session_factory: SessionFactory,
        provider_factory: ProviderFactory,
    ) -> None:
        self._settings = settings
        self._factory = session_factory
        self.provider_factory = provider_factory
        self._limiter = SlidingWindowLimiter(settings.tool_calls_per_minute, 60)
        self._breaker = CircuitBreaker(
            settings.tool_breaker_failures, settings.tool_breaker_cooldown_seconds
        )

    async def discover(self, actor: AdminActor, server_id: uuid.UUID) -> DiscoveryReport:
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            config = server_config(await get_server(session, server_id))
        timeout = self._settings.tool_default_timeout_seconds
        try:
            descriptors = await self.provider_factory(config).discover(timeout_seconds=timeout)
        except ToolProviderError as exc:
            async with tenant_scope(self._factory, actor.workspace_id) as session:
                self._mark_server_failure(await get_server(session, server_id), exc)
                record_event(
                    session,
                    "tool_server.discover",
                    workspace_id=actor.workspace_id,
                    actor_user_id=actor.user_id,
                    target_type="tool_server",
                    target_id=server_id,
                    outcome="failure",
                    metadata={"code": exc.code},
                )
            raise ToolUnavailable(
                "The tool server could not be reached, so no tools were updated",
                details={"code": exc.code},
            ) from exc
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            return await apply_discovery(
                session,
                actor,
                server_id,
                descriptors,
                max_schema_bytes=self._settings.tool_max_schema_bytes,
            )

    async def invoke(
        self, actor: ToolActor, tool_definition_id: uuid.UUID, arguments: dict[str, Any]
    ) -> InvocationResult:
        require_permission(actor.role, Permission.TOOLS_INVOKE)
        plan, refusal = await self._authorize(actor, tool_definition_id, arguments)
        if refusal is not None:
            # Raised here rather than inside the transaction, so the denial record commits.
            raise refusal
        assert plan is not None  # noqa: S101 - _authorize returns exactly one of the two

        started = time.perf_counter()
        key = str(plan.server.id)
        try:
            provider = self.provider_factory(plan.server)

            async def attempt(remaining: float) -> ToolOutput:
                return await provider.invoke(
                    plan.name,
                    arguments,
                    expected_hash=plan.definition_hash,
                    timeout_seconds=remaining,
                )

            # Only read tools reach this point, so a retry cannot repeat a side effect.
            outcome = await call_with_retry(
                attempt,
                budget_seconds=plan.timeout_seconds,
                retries=self._settings.tool_read_retries,
                base_delay_seconds=self._settings.tool_retry_base_seconds,
            )
        except DefinitionChanged as exc:
            await self._finish_changed(actor, tool_definition_id, plan, exc, started)
            raise ToolDefinitionChanged(
                "This tool's definition changed on the server, so it was not called and has "
                "been disabled until an admin reviews it"
            ) from exc
        except ToolMissing as exc:
            await self._finish_missing(actor, tool_definition_id, plan, exc, started)
            raise ToolUnavailable(
                "The server no longer offers this tool. It was not called.",
                details={"code": exc.code},
            ) from exc
        except ToolProviderError as exc:
            self._breaker.record_failure(key)
            await self._finish_failed(actor, plan, exc, started)
            raise ToolUnavailable(
                "The tool call did not complete, so no live data was retrieved",
                details={"code": exc.code},
            ) from exc

        self._breaker.record_success(key)
        return await self._finish_ok(actor, plan, outcome.value, outcome.attempts, started)

    async def _authorize(
        self, actor: ToolActor, tool_definition_id: uuid.UUID, arguments: dict[str, Any]
    ) -> tuple[_Plan | None, AppError | None]:
        settings = self._settings
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            # NotFound raises through: another workspace's tool has nothing to record against.
            tool, server = await get_tool(session, tool_definition_id)

            refusal: AppError | None = None
            code: str | None = not_callable_reason(tool, server)
            if code is not None:
                refusal = ToolNotCallable("This tool cannot be called", details={"reason": code})
            if refusal is None:
                try:
                    validate_arguments(
                        tool.input_schema, arguments, max_bytes=settings.tool_max_argument_bytes
                    )
                except AppError as exc:
                    refusal, code = exc, "invalid_arguments"
            limiter_key = f"{actor.user_id}:{server.id}"
            if refusal is None:
                try:
                    self._limiter.check(limiter_key)
                except AppError as exc:
                    refusal, code = exc, "rate_limited"
            if refusal is None and not self._breaker.allow(str(server.id)):
                code = "circuit_open"
                refusal = ToolUnavailable(
                    "The tool server is failing and calls are paused, so no live data was "
                    "retrieved",
                    details={"code": code},
                )

            invocation = ToolInvocation(
                workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id,
                tool_definition_id=tool.id,
                server_id=server.id,
                tool_id=tool.tool_id,
                definition_hash=tool.definition_hash,
                arguments_hash=_arguments_hash(arguments),
                status=InvocationStatus.DENIED if refusal else InvocationStatus.RUNNING,
                error_code=code,
                attempts=0,
                finished_at=utcnow() if refusal else None,
            )
            session.add(invocation)
            await session.flush()
            self._audit(
                session,
                actor,
                invocation,
                "tool.invoke",
                outcome="denied" if refusal else "started",
            )
            if refusal is not None:
                return None, refusal
            self._limiter.hit(limiter_key)
            return (
                _Plan(
                    invocation_id=invocation.id,
                    tool_id=tool.tool_id,
                    server=server_config(server),
                    name=tool.name,
                    definition_hash=tool.definition_hash,
                    timeout_seconds=min(
                        tool.timeout_seconds or settings.tool_default_timeout_seconds,
                        settings.tool_max_timeout_seconds,
                    ),
                ),
                None,
            )

    @staticmethod
    def _audit(
        session: Any,
        actor: ToolActor,
        invocation: ToolInvocation,
        action: str,
        *,
        outcome: str,
    ) -> None:
        record_event(
            session,
            action,
            workspace_id=actor.workspace_id,
            actor_user_id=actor.user_id,
            target_type="tool_invocation",
            target_id=invocation.id,
            outcome=outcome,
            metadata={"tool_id": invocation.tool_id, "code": invocation.error_code},
        )

    @staticmethod
    def _mark_server_failure(server: ToolServer, exc: ToolProviderError) -> None:
        server.health = ServerHealth.UNAVAILABLE
        server.last_error_code = exc.code
        server.last_checked_at = utcnow()

    async def _close(
        self,
        session: Any,
        actor: ToolActor,
        plan: _Plan,
        *,
        status: InvocationStatus,
        code: str | None,
        attempts: int,
        started: float,
        output: ToolOutput | None = None,
    ) -> float:
        latency_ms = (time.perf_counter() - started) * 1000
        invocation = await session.get(ToolInvocation, plan.invocation_id)
        invocation.status = status
        invocation.error_code = code
        invocation.attempts = attempts
        invocation.latency_ms = latency_ms
        invocation.finished_at = utcnow()
        if output is not None:
            invocation.output_chars = output.output_chars
            invocation.truncated = output.truncated
        self._audit(
            session,
            actor,
            invocation,
            "tool.invoke.result",
            outcome="success" if status is InvocationStatus.SUCCEEDED else "failure",
        )
        return latency_ms

    async def _finish_ok(
        self,
        actor: ToolActor,
        plan: _Plan,
        output: ToolOutput,
        attempts: int,
        started: float,
    ) -> InvocationResult:
        status = InvocationStatus.TOOL_ERROR if output.is_error else InvocationStatus.SUCCEEDED
        fetched_at = utcnow()
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            latency_ms = await self._close(
                session,
                actor,
                plan,
                status=status,
                code="tool_error" if output.is_error else None,
                attempts=attempts,
                started=started,
                output=output,
            )
            await session.execute(
                update(ToolServer)
                .where(ToolServer.id == plan.server.id)
                .values(
                    health=ServerHealth.HEALTHY, last_error_code=None, last_checked_at=fetched_at
                )
            )
        return InvocationResult(
            plan.invocation_id, plan.tool_id, status, output, fetched_at, latency_ms, attempts
        )

    async def _finish_failed(
        self, actor: ToolActor, plan: _Plan, exc: ToolProviderError, started: float
    ) -> None:
        log.warning("tool_call_failed", tool_id=plan.tool_id, code=exc.code)
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            await self._close(
                session,
                actor,
                plan,
                status=InvocationStatus.FAILED,
                code=exc.code,
                attempts=getattr(exc, "attempts", 1),
                started=started,
            )
            self._mark_server_failure(await get_server(session, plan.server.id), exc)

    async def _finish_changed(
        self,
        actor: ToolActor,
        tool_definition_id: uuid.UUID,
        plan: _Plan,
        exc: DefinitionChanged,
        started: float,
    ) -> None:
        log.warning("tool_definition_changed", tool_id=plan.tool_id)
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            await self._close(
                session,
                actor,
                plan,
                status=InvocationStatus.DENIED,
                code=exc.code,
                attempts=getattr(exc, "attempts", 1),
                started=started,
            )
            tool = await session.get(ToolDefinition, tool_definition_id)
            if tool is not None:
                apply_definition_change(tool, exc.current)
            record_event(
                session,
                "tool.definition_changed",
                workspace_id=actor.workspace_id,
                actor_user_id=actor.user_id,
                target_type="tool",
                target_id=tool_definition_id,
                outcome="denied",
                metadata={"tool_id": plan.tool_id, "new_hash": exc.current.definition_hash},
            )

    async def _finish_missing(
        self,
        actor: ToolActor,
        tool_definition_id: uuid.UUID,
        plan: _Plan,
        exc: ToolMissing,
        started: float,
    ) -> None:
        async with tenant_scope(self._factory, actor.workspace_id) as session:
            await self._close(
                session,
                actor,
                plan,
                status=InvocationStatus.FAILED,
                code=exc.code,
                attempts=getattr(exc, "attempts", 1),
                started=started,
            )
            tool = await session.get(ToolDefinition, tool_definition_id)
            if tool is not None:
                tool.available = False
