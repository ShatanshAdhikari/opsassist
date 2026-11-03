"""Controlled tool execution: registry, schema validation, policy, invocation and audit.

Callers go through `gateway.ToolGateway`. Transports (MCP today, native adapters later) sit
behind `base.ToolProvider`, so nothing above this package knows how a tool is reached.
"""
