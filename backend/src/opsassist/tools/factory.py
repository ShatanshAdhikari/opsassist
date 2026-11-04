"""Builds a provider for a registered server."""

from __future__ import annotations

from collections.abc import Callable

from mcp import Client, StdioServerParameters

from opsassist.config import Settings
from opsassist.integrations.github_provider import GitHubDeps, GitHubToolProvider
from opsassist.integrations.gmail_provider import GmailDeps, GmailToolProvider
from opsassist.tools.base import ProviderBlocked, ServerConfig, ServerKind, ToolProvider
from opsassist.tools.mcp_provider import CLIENT_INFO, McpToolProvider
from opsassist.tools.url_policy import BlockedUrl, check_server_url

ProviderFactory = Callable[[ServerConfig], ToolProvider]


def build_provider_factory(
    settings: Settings, github: GitHubDeps | None = None, gmail: GmailDeps | None = None
) -> ProviderFactory:
    allowed_hosts = frozenset(host.lower() for host in settings.tool_allowed_private_hosts)

    def _client(target: str | StdioServerParameters, timeout: float) -> Client:
        return Client(target, read_timeout_seconds=timeout, client_info=CLIENT_INFO, cache=None)

    def build(server: ServerConfig) -> ToolProvider:
        max_tools = settings.tool_max_discovered
        max_output_chars = settings.tool_max_output_chars
        if server.kind is ServerKind.GITHUB:
            if github is None or server.connection_id is None:
                raise ProviderBlocked()
            return GitHubToolProvider(github, server.workspace_id, server.connection_id)
        if server.kind is ServerKind.GMAIL:
            if gmail is None or server.connection_id is None:
                raise ProviderBlocked()
            return GmailToolProvider(gmail, server.workspace_id, server.connection_id)
        if server.kind is ServerKind.MCP_HTTP:
            if server.url is None:
                raise ProviderBlocked()
            url = server.url

            async def check() -> None:
                try:
                    await check_server_url(url, allowed_private_hosts=allowed_hosts)
                except BlockedUrl as exc:
                    raise ProviderBlocked() from exc

            return McpToolProvider(
                lambda timeout: _client(url, timeout),
                max_tools=max_tools,
                max_output_chars=max_output_chars,
                pre_connect=check,
            )

        # Commands come from deployment configuration only, never from an API request.
        configured = settings.mcp_stdio_servers.get(server.stdio_name or "")
        if configured is None:
            raise ProviderBlocked()
        # The SDK merges `env` over a minimal default, so this process's secrets are not inherited.
        parameters = StdioServerParameters(
            command=configured.command, args=configured.args, env=configured.env
        )
        return McpToolProvider(
            lambda timeout: _client(parameters, timeout),
            max_tools=max_tools,
            max_output_chars=max_output_chars,
        )

    return build
