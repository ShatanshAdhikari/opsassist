"""Measure what one MCP tool call costs through McpToolProvider (ADR-0009).

Each call opens a connection and lists tools to check the pinned definition. This reports that
cost against the local mock server, so it is transport and SDK overhead with no network distance
and no real tool work. Run from backend/: `python -m evals.measure_tool_latency`.
"""

from __future__ import annotations

import asyncio
import socket
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from mcp import Client, StdioServerParameters
from tests.mock_mcp_server import build_server

from opsassist.tools.base import ToolDescriptor
from opsassist.tools.mcp_provider import CLIENT_INFO, McpToolProvider

MOCK_SERVER = Path(__file__).resolve().parents[1] / "tests" / "mock_mcp_server.py"
_MAX_OUTPUT_CHARS = 200_000
_TIMEOUT = 30.0


async def _measure(label: str, connect: Callable[[float], Client], calls: int) -> None:
    provider = McpToolProvider(connect, max_tools=200, max_output_chars=_MAX_OUTPUT_CHARS)
    tools: list[ToolDescriptor] = await provider.discover(timeout_seconds=_TIMEOUT)
    echo = next(tool for tool in tools if tool.name == "echo")
    samples: list[float] = []
    for _ in range(calls):
        started = time.perf_counter()
        await provider.invoke(
            "echo",
            {"text": "ping"},
            expected_hash=echo.definition_hash,
            timeout_seconds=_TIMEOUT,
        )
        samples.append((time.perf_counter() - started) * 1000)
    samples.sort()
    print(
        f"{label:<12} n={calls:<4} p50={statistics.median(samples):8.1f} ms  "
        f"p95={samples[int(len(samples) * 0.95) - 1]:8.1f} ms  max={samples[-1]:8.1f} ms"
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


async def main() -> None:
    server = build_server()

    def in_process(timeout: float) -> Client:
        return Client(server, read_timeout_seconds=timeout, client_info=CLIENT_INFO, cache=None)

    await _measure("in-process", in_process, 200)

    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    # Blocking calls are fine here: nothing else runs on this loop between measurements.
    process = subprocess.Popen([sys.executable, str(MOCK_SERVER), "http", str(port)])  # noqa: ASYNC220
    try:
        for _ in range(100):
            try:
                httpx.post(url, timeout=1.0)  # noqa: ASYNC210
                break
            except httpx.TransportError:
                await asyncio.sleep(0.1)

        def over_http(timeout: float) -> Client:
            return Client(url, read_timeout_seconds=timeout, client_info=CLIENT_INFO, cache=None)

        await _measure("http", over_http, 100)
    finally:
        process.terminate()
        process.wait(timeout=10)

    parameters = StdioServerParameters(command=sys.executable, args=[str(MOCK_SERVER)])

    def over_stdio(timeout: float) -> Client:
        return Client(parameters, read_timeout_seconds=timeout, client_info=CLIENT_INFO, cache=None)

    await _measure("stdio", over_stdio, 20)


if __name__ == "__main__":
    asyncio.run(main())
