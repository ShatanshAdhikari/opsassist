"""Run a plan's calls through the gateway and report honestly what each one did.

Calls run one at a time. The deadline is checked between calls rather than by cancelling one, so a
call in progress finishes under the gateway's own timeout and its invocation record gets closed.
Failures stay separate from evidence: a failed or tool-reported error supplies no text to the model.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from opsassist.assistant.planner import PlannedCall
from opsassist.errors import AppError
from opsassist.observability import get_logger, redact_secrets, redact_tree
from opsassist.tools.base import InvocationStatus, ToolOutput
from opsassist.tools.gateway import ToolActor, ToolGateway

log = get_logger("opsassist.assistant")

STATUS_SKIPPED = "skipped"


@dataclass(frozen=True)
class ToolEvidence:
    """Output from one successful call. Untrusted: whatever the external system returned."""

    tool_id: str
    server_id: uuid.UUID
    invocation_id: uuid.UUID
    arguments: dict[str, Any]
    fetched_at: datetime
    content: str
    truncated: bool


@dataclass(frozen=True)
class ToolCallRecord:
    tool_id: str
    arguments: dict[str, Any]
    status: str
    code: str | None = None
    invocation_id: uuid.UUID | None = None
    fetched_at: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "tool_id": self.tool_id,
            "arguments": self.arguments,
            "status": self.status,
            "code": self.code,
            "invocation_id": str(self.invocation_id) if self.invocation_id else None,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
        }


def _content(output: ToolOutput) -> str:
    if output.structured is not None:
        return json.dumps(output.structured, ensure_ascii=False)
    return output.text


def _failure_code(exc: AppError) -> str:
    code = exc.details.get("code") or exc.details.get("reason") or exc.code
    return str(code)


def _status_of_refusal(exc: AppError) -> str:
    return str(
        InvocationStatus.FAILED if exc.status_code >= 500 else InvocationStatus.DENIED,
    )


async def run_calls(
    gateway: ToolGateway,
    actor: ToolActor,
    calls: tuple[PlannedCall, ...],
    *,
    deadline: float,
    max_context_chars: int,
) -> tuple[list[ToolCallRecord], list[ToolEvidence]]:
    records: list[ToolCallRecord] = []
    evidence: list[ToolEvidence] = []
    for call in calls:
        arguments = redact_tree(call.arguments)
        tool_id = call.tool.tool_id
        if time.monotonic() >= deadline:
            records.append(ToolCallRecord(tool_id, arguments, STATUS_SKIPPED, "time_limit"))
            continue
        try:
            result = await gateway.invoke(actor, call.tool.definition_id, call.arguments)
        except AppError as exc:
            code = _failure_code(exc)
            log.info("planned_tool_call_refused", tool_id=tool_id, code=code)
            records.append(ToolCallRecord(tool_id, arguments, _status_of_refusal(exc), code))
            continue
        records.append(
            ToolCallRecord(
                tool_id,
                arguments,
                str(result.status),
                "tool_error" if result.status is InvocationStatus.TOOL_ERROR else None,
                result.invocation_id,
                result.fetched_at,
            )
        )
        if result.status is InvocationStatus.SUCCEEDED:
            evidence.append(
                ToolEvidence(
                    tool_id=tool_id,
                    server_id=call.tool.server_id,
                    invocation_id=result.invocation_id,
                    arguments=arguments,
                    fetched_at=result.fetched_at,
                    content=redact_secrets(_content(result.output)),
                    truncated=result.output.truncated,
                )
            )
    return records, _fit_to_budget(evidence, max_context_chars)


def _fit_to_budget(evidence: list[ToolEvidence], max_chars: int) -> list[ToolEvidence]:
    """Give each result an equal share, marking any that were cut as incomplete."""
    if not evidence:
        return []
    share = max_chars // len(evidence)
    return [
        replace(item, content=item.content[:share], truncated=True)
        if len(item.content) > share
        else item
        for item in evidence
    ]
