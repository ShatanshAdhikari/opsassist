# Integration Strategy

Status: draft for review, 2026-11-03. Nothing in this document is built yet except
the model provider adapters. Phase 3 implements it.

## 1. One contract, two transports

All capabilities reach the orchestrator through one **Tool Gateway** whether they
are MCP servers or native adapters. Callers never see the transport.

```python
class ToolDefinition:          # persisted, admin-reviewed, pinned
    tool_id: str               # stable: "github.issues.search"
    provider: str              # "github"; server identity for MCP
    description: str           # reviewed text, shown to models
    input_schema: dict         # JSON Schema; additionalProperties false
    risk_level: 0 | 1 | 2 | 3
    access: "read" | "write"
    required_scopes: list[str]
    timeout_s: float
    retry: RetryPolicy         # writes default to no automatic retry
    idempotency: "natural" | "key" | "none"
    enabled: bool
    health: "healthy" | "degraded" | "unavailable"

class ToolProvider(Protocol):
    async def discover(self) -> list[ToolDescriptor]: ...
    async def invoke(self, tool_id, args, credential, idempotency_key) -> ToolResult: ...
    async def verify(self, tool_id, args, idempotency_key) -> VerifyResult: ...   # reconcile writes
    async def health(self) -> Health: ...
```

## 2. When MCP, when native

| Use | When |
| --- | --- |
| Official MCP server | Vendor-maintained, scopes can be narrowed, transport supports per-user credentials |
| Native adapter | High-consequence writes needing idempotency or read-back; bulk sync for RAG; provider webhooks; MCP server cannot be scoped |
| Own MCP server | Internal systems we want reusable outside this platform |

Bulk ingestion for RAG always uses native sync, since it needs cursors, deletes and
permission metadata that the MCP tool model does not express well. MCP `resources`
may be used for on-demand reads.

## 3. First integrations

### GitHub, read-only (Phase 3)
- Auth: **GitHub App** with per-repository installation, not a personal token. Permissions: `metadata:read, contents:read, issues:read, pull_requests:read, checks:read`.
- Why: per-repo scoping, short-lived installation tokens, higher rate limits.
- Acting-user check: an installation token sees everything installed, so the gateway must also verify the *requesting user's* repo access via their linked OAuth identity. Otherwise a user could read a private repo through the app. This is a design risk to validate before shipping.
- Live state (PR status, checks) always comes from the API, never the index. Writes join in Phase 4 as separate tools at Level 2 or 3.

### Email, read-only (Phase 3)
- Gmail first, behind `MailProvider`. Scope `gmail.readonly` only.
- **Risk**: Google classifies `gmail.readonly` as a restricted scope. Production use outside a single Workspace domain requires Google verification and possibly a security assessment. Internal-only OAuth apps in one Workspace avoid that. See open decision in `IMPLEMENTATION_PLAN.md`.
- Email bodies, attachments and quoted text are hostile input. Attachments are not ingested until scanning and limits exist. HTML is reduced to text before reaching a model or browser.
- Later: `gmail.compose` for drafts (Level 1). `gmail.send` only as a separately granted Level 2 tool with duplicate-reply check.

## 4. Credentials

`integration_connections` holds metadata and a `credential_ref`. Tokens live in an
encrypted store behind a `CredentialStore` interface. Refresh, rotation and revocation
are part of the connector's definition of done.

## 5. Reliability contract

Every provider call goes through shared middleware: timeout, per-connection rate
limit honouring `Retry-After`, exponential backoff with jitter (reads only by default),
circuit breaker per provider, and output size cap.

Writes: require an idempotency key. If the result is uncertain, call `verify` and
never replay blindly. No `verify` means the action is reported as `outcome_unknown`
for human reconciliation.

## 6. MCP lifecycle

1. Admin registers server (URL or allowlisted stdio command), identity, credentials.
2. Discovery snapshot stored; each tool imported **disabled**, risk unclassified treated as Level 3.
3. Admin reviews, classifies and enables tools.
4. Periodic re-discovery. Any hash change in schema or description disables the tool and alerts.
5. Disconnection marks tools unavailable; the orchestrator reports that live state was not checked.

## 7. Test strategy

Mock MCP server in the test suite (official SDK); recorded-shape fake HTTP servers for
native adapters; dedicated sandbox accounts for selected end-to-end tests; never real
production data by default.

## 8. Adding an integration: checklist

Provider adapter or MCP registration; tool definitions with risk classification; minimal
scopes documented; authorization, refresh and revocation tested; rate limit and outage
tested; injection fixtures for its content; audit events verified; docs updated.
