"""Tool contract shared by every transport."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class ToolAccess(StrEnum):
    READ = "read"
    WRITE = "write"


class ReviewStatus(StrEnum):
    PENDING = "pending"  # Discovered, never reviewed.
    APPROVED = "approved"
    CHANGED = "changed"  # Provider changed an approved definition. Needs a new review.


class ServerKind(StrEnum):
    MCP_HTTP = "mcp_http"
    MCP_STDIO = "mcp_stdio"
    GITHUB = "github"
    GMAIL = "gmail"


class ServerHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNAVAILABLE = "unavailable"


class InvocationStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    TOOL_ERROR = "tool_error"  # The tool ran and reported failure.
    FAILED = "failed"  # The call did not complete, so the outcome is not a tool answer.
    DENIED = "denied"  # Refused before any provider was contacted.


# Unreviewed tools are treated as the most dangerous kind until an admin says otherwise.
UNCLASSIFIED_RISK_LEVEL = 3
MAX_RISK_LEVEL = 3
READ_ONLY_RISK_LEVEL = 0


@dataclass(frozen=True)
class ToolDescriptor:
    """What a provider claims about a tool. Untrusted until an admin has reviewed it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    def as_definition(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "annotations": self.annotations,
        }

    @property
    def definition_hash(self) -> str:
        return hash_definition(self.as_definition())


def hash_definition(definition: dict[str, Any]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class ToolOutput:
    """Provider output after size limits and redaction. Always untrusted data."""

    text: str
    structured: dict[str, Any] | None
    is_error: bool
    truncated: bool = False
    omitted_blocks: int = 0

    @property
    def output_chars(self) -> int:
        structured_size = len(json.dumps(self.structured)) if self.structured is not None else 0
        return len(self.text) + structured_size


@dataclass(frozen=True)
class ServerConfig:
    """Detached copy of a server row, safe to use after its transaction has closed."""

    id: Any
    slug: str
    kind: ServerKind
    url: str | None
    stdio_name: str | None
    workspace_id: Any = None
    connection_id: Any = None


class ToolProviderError(Exception):
    """A provider call failed. `code` is safe to store and return; messages stay internal."""

    code = "provider_error"
    retryable = False


class ProviderUnreachable(ToolProviderError):
    code = "server_unavailable"
    retryable = True


class ProviderTimeout(ToolProviderError):
    code = "timeout"
    retryable = True


class ProviderProtocolError(ToolProviderError):
    """The server answered, but not with a usable result."""

    code = "protocol_error"


class ProviderBlocked(ToolProviderError):
    """Policy refused the connection, for example a URL resolving to a private address."""

    code = "url_blocked"


class ProviderRateLimited(ToolProviderError):
    """The provider asked us to wait. Retried only if the wait fits the call's budget."""

    code = "rate_limited"
    retryable = True

    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__(self.code)
        self.retry_after_seconds = retry_after_seconds


class CredentialRejected(ToolProviderError):
    """The stored credential is missing, unusable or refused. Needs an admin, not a retry."""

    code = "credential_rejected"


class ReauthorizationRequired(CredentialRejected):
    """The person's sign-in expired or was revoked. Only they can fix it, by connecting again."""

    code = "reauthorization_required"


class DefinitionChanged(ToolProviderError):
    """The live definition differs from the pinned one, so the tool was not called."""

    code = "definition_changed"

    def __init__(self, current: ToolDescriptor) -> None:
        super().__init__(self.code)
        self.current = current


class ToolMissing(ToolProviderError):
    """The server no longer offers this tool."""

    code = "tool_missing"


class ToolProvider(Protocol):
    async def discover(self, *, timeout_seconds: float) -> list[ToolDescriptor]: ...

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_hash: str,
        timeout_seconds: float,
    ) -> ToolOutput:
        """Call the tool only if its live definition still matches `expected_hash`."""
        ...
