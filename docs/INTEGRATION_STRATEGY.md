# Integration Strategy

Status, 2026-11-04: the tool layer in sections 1, 5 and 6 is built (`backend/src/opsassist/tools/`),
with MCP over HTTP and stdio, tested against a mock server. Credentials (section 4) and read-only
GitHub (section 3) are built, and tested against a fake GitHub until the live test in
`GITHUB_SETUP.md` has been run: see `PROGRESS.md`. Email and calendar are still design. Section 6
marks what differs from the original plan.

## 1. One contract, two transports

All capabilities reach the orchestrator through one **Tool Gateway** whether they
are MCP servers or native adapters. Callers never see the transport.

Built today (`tools/base.py`, `models.py`). `invoke` compares the live definition with the pinned
hash before calling. There is no retry policy or idempotency column yet, because only read tools
run and those retry by default.

```python
class ToolDefinition:          # persisted, admin-reviewed, pinned
    tool_id: str               # stable: "<server slug>.<tool name>"
    server_id: UUID            # provider identity
    description: str           # reviewed text; a newer version waits in pending_definition
    input_schema: dict         # JSON Schema; unknown top-level fields are always refused
    risk_level: 0 | 1 | 2 | 3
    access: "read" | "write"
    required_scopes: list[str] # recorded; enforced once credentials exist
    timeout_seconds: float | None
    review_status: "pending" | "approved" | "changed"
    enabled: bool
    available: bool            # the server still offers it

class ToolProvider(Protocol):
    async def discover(self, *, timeout_seconds) -> list[ToolDescriptor]: ...
    async def invoke(self, name, arguments, *, expected_hash, timeout_seconds) -> ToolOutput: ...
```

Planned additions for Phase 4 writes: `credential`, `idempotency_key`, a `verify` method to
reconcile uncertain writes, and per-tool `retry` and `idempotency` settings.

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
- Tools: `issues_search`, `issue_get`, `pulls_list`, `pull_get`, `file_get`. A native provider, reviewed and pinned like any MCP tool.
- Scoping is layered: tokens are requested read-only and limited to the connection's repositories; a repository outside the list is refused before any request; search refuses `repo:`, `org:`, `user:` and `owner:`, and drops results from elsewhere; redirects are not followed.
- **Per-person access is not checked.** An installation token sees everything installed, so any member with `tools:invoke` can read every repository on the connection. An admin choosing the list is the control today (ADR-0012). Checking each person's access through a linked GitHub identity is planned before this suits private repositories with different audiences.
- Live state (PR status, checks) always comes from the API, never the index. Writes join in Phase 4 as separate tools at Level 2 or 3.

### Email, read-only (Phase 3)
- Gmail first, behind `MailProvider`. Scope `gmail.readonly` only.
- **Risk**: Google classifies `gmail.readonly` as a restricted scope. Production use outside a single Workspace domain requires Google verification and possibly a security assessment. Internal-only OAuth apps in one Workspace avoid that. See open decision in `IMPLEMENTATION_PLAN.md`.
- Email bodies, attachments and quoted text are hostile input. Attachments are not ingested until scanning and limits exist. HTML is reduced to text before reaching a model or browser.
- Later: `gmail.compose` for drafts (Level 1). `gmail.send` only as a separately granted Level 2 tool with duplicate-reply check.

## 4. Credentials

Built. `integration_connections` holds settings and a `credential_id`. Secrets live in
`stored_credentials`, encrypted with a per-secret key wrapped by a key from deployment
configuration (ADR-0011). Installation tokens stay in memory only. Replacing a key, rotating the
encryption key and deleting a connection all exist: see `GITHUB_SETUP.md`.

Refresh and revocation for OAuth providers such as Gmail are not built.

## 5. Reliability contract

Built: one timeout budget covering all attempts, per-user-per-server rate limit, exponential
backoff with jitter (reads only), circuit breaker per server, output size cap. Rate limiter and
breaker state are per process.

Planned: honouring a provider's `Retry-After`. MCP does not surface it, so this arrives with the
native adapters.

Writes: require an idempotency key. If the result is uncertain, call `verify` and
never replay blindly. No `verify` means the action is reported as `outcome_unknown`
for human reconciliation.

## 6. MCP lifecycle

1. **Built.** Admin registers a server: a URL passing the policy in `tools/url_policy.py`, or the
   name of a stdio server from deployment configuration. **No credentials yet**, so only servers
   needing no authentication work, and URLs containing credentials are refused.
2. **Built.** Admin triggers discovery. Each tool is imported **disabled**, unclassified, at Level 3.
   Definitions that are invalid or hold non-local schema references are rejected and reported.
3. **Built.** Admin reviews, classifies and enables. The review names the hash of the definition
   shown; a stale hash is refused.
4. **Built, differently from the plan.** Instead of periodic re-discovery, every invocation lists
   the tools and compares hashes before calling, so a changed tool is never called. This covers
   tools in use. A scheduled sweep and admin alerts are still planned, so today an admin learns of
   a change from the audit trail or the tool list.
5. **Built.** Tool server health is recorded on each call. Failures return `tool_unavailable` with
   a message saying no live data was retrieved. Nothing above the gateway consumes this yet.

Only read tools at Level 0 run. Everything else is refused with `requires_approval_flow` until the
approval flow of Phase 4 exists, however an admin classifies it.

## 7. Test strategy

Mock MCP server in the test suite (official SDK); recorded-shape fake HTTP servers for
native adapters; dedicated sandbox accounts for selected end-to-end tests; never real
production data by default.

## 8. Adding an integration: checklist

Provider adapter or MCP registration; tool definitions with risk classification; minimal
scopes documented; authorization, refresh and revocation tested; rate limit and outage
tested; injection fixtures for its content; audit events verified; docs updated.
