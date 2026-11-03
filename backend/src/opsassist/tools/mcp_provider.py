"""MCP transport for the tool contract, using the official SDK.

Connects per operation. That costs a handshake per call, but there is no connection state to lose
when a server restarts, and every call can compare the live definition with the pinned one. Move
to pooling only if measured latency requires it.

The client declares no sampling, elicitation or roots callbacks, so a server cannot ask this
process for model completions, user input or file paths. Response caching is off because a cached
tool list would defeat the definition check.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import anyio
from mcp import Client, MCPError
from mcp.types import CallToolResult, Implementation, TextContent, Tool
from pydantic import ValidationError

from opsassist.observability import get_logger, redact_secrets, redact_tree
from opsassist.tools.base import (
    DefinitionChanged,
    ProviderProtocolError,
    ProviderTimeout,
    ProviderUnreachable,
    ToolDescriptor,
    ToolMissing,
    ToolOutput,
    ToolProviderError,
)

log = get_logger("opsassist.tools.mcp")

ClientFactory = Callable[[float], Client]
PreConnectCheck = Callable[[], Awaitable[None]]

CLIENT_INFO = Implementation(name="opsassist", version="0.1.0")

_MAX_LIST_PAGES = 20


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc


def _classify(exc: Exception) -> ToolProviderError:
    """Map SDK and transport failures. Their text can hold server content, so it stays in logs."""
    leaves = list(_leaves(exc))
    if any(isinstance(leaf, TimeoutError) for leaf in leaves):
        return ProviderTimeout()
    if any(isinstance(leaf, MCPError | ValidationError) for leaf in leaves):
        return ProviderProtocolError()
    return ProviderUnreachable()


def _descriptor(tool: Tool) -> ToolDescriptor:
    annotations = (
        tool.annotations.model_dump(mode="json", exclude_none=True) if tool.annotations else {}
    )
    return ToolDescriptor(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.input_schema,
        annotations=annotations,
    )


async def _list_tools(client: Client, *, max_tools: int) -> list[ToolDescriptor]:
    tools: list[ToolDescriptor] = []
    cursor: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        page = await client.list_tools(cursor=cursor)
        tools.extend(_descriptor(tool) for tool in page.tools)
        if len(tools) > max_tools:
            raise ProviderProtocolError()
        cursor = page.next_cursor
        if not cursor:
            return tools
    # A server that never stops paginating is misbehaving, not merely large.
    raise ProviderProtocolError()


def _to_output(result: CallToolResult, *, max_chars: int) -> ToolOutput:
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    omitted = len(result.content) - len(texts)
    text = "\n".join(texts)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    structured = result.structured_content
    if structured is not None and len(json.dumps(structured)) > max_chars:
        structured = None
        truncated = True
    return ToolOutput(
        text=redact_secrets(text),
        structured=redact_tree(structured) if structured is not None else None,
        is_error=bool(result.is_error),
        truncated=truncated,
        omitted_blocks=omitted,
    )


class McpToolProvider:
    def __init__(
        self,
        connect: ClientFactory,
        *,
        max_tools: int,
        max_output_chars: int,
        pre_connect: PreConnectCheck | None = None,
    ) -> None:
        self._connect = connect
        self._max_tools = max_tools
        self._max_output_chars = max_output_chars
        self._pre_connect = pre_connect

    async def discover(self, *, timeout_seconds: float) -> list[ToolDescriptor]:
        if self._pre_connect:
            await self._pre_connect()
        try:
            with anyio.fail_after(timeout_seconds):
                async with self._connect(timeout_seconds) as client:
                    return await _list_tools(client, max_tools=self._max_tools)
        except ToolProviderError:
            raise
        except Exception as exc:
            raise self._failure("discover", exc) from exc

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_hash: str,
        timeout_seconds: float,
    ) -> ToolOutput:
        if self._pre_connect:
            await self._pre_connect()
        # Decisions leave the `async with` as values: raising inside the SDK's task groups would
        # come back wrapped in an ExceptionGroup.
        current: ToolDescriptor | None = None
        missing = False
        result: CallToolResult | None = None
        try:
            with anyio.fail_after(timeout_seconds):
                async with self._connect(timeout_seconds) as client:
                    tools = await _list_tools(client, max_tools=self._max_tools)
                    current = next((tool for tool in tools if tool.name == name), None)
                    missing = current is None
                    if current is not None and current.definition_hash == expected_hash:
                        result = await client.call_tool(name, arguments)
        except ToolProviderError:
            raise
        except Exception as exc:
            raise self._failure("invoke", exc) from exc
        if missing:
            raise ToolMissing()
        if result is None:
            assert current is not None  # noqa: S101 - narrowed by the branches above
            raise DefinitionChanged(current)
        return _to_output(result, max_chars=self._max_output_chars)

    @staticmethod
    def _failure(operation: str, exc: Exception) -> ToolProviderError:
        failure = _classify(exc)
        log.warning(
            "mcp_call_failed",
            operation=operation,
            code=failure.code,
            error_types=sorted({type(leaf).__name__ for leaf in _leaves(exc)}),
        )
        return failure
