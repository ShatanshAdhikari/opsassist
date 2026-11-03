# Security Model

Status: draft for review, 2026-11-03. Each control is marked **[built]** or
**[planned]**. A control marked built has a test unless noted.

## 1. Assets

Organizational documents and chunks; provider credentials *(none stored yet)*;
sessions and password hashes; audit trail; the ability to read from registered tool servers;
the ability to act on external systems *(none yet: write tools are refused)*.

## 2. Threat actors

| Actor | Example |
| --- | --- |
| External content author | Malicious email or issue that instructs the model |
| Authenticated user beyond their grant | Asks for restricted source content |
| Member of another tenant | Guesses workspace or document IDs |
| Compromised or hostile MCP server | Rewrites tool description, returns injected output |
| Curious operator | Reads secrets out of logs |
| Model itself | Hallucinates a citation or proposes a harmful action |

The model is treated as **an untrusted component**. It influences text, never
authorization.

## 3. Authentication and sessions

- [built] Argon2id password hashes. Minimum 12 characters.
- [built] Opaque 256-bit session tokens; only the SHA-256 hash is stored. Idle and absolute expiry; logout revokes server-side.
- [built] Cookie `HttpOnly`, `SameSite=Lax`, `Secure` when `COOKIE_SECURE=true` (default true; dev `.env.example` disables it for plain-HTTP localhost).
- [built] CSRF: unsafe methods require an allowlisted `Origin`, in addition to SameSite.
- [built] Login throttling by account and IP. In-process only, so per-replica; shared store needed before horizontal scale.
- [built] Uniform login failure response with dummy hash verification to reduce user enumeration.
- [planned] OIDC/SAML SSO, MFA, password reset flow.

## 4. Authorization

- [built] Roles: `owner`, `admin`, `member`, `viewer`, mapped to explicit permissions in code.
- [built] Non-members receive 404.
- [built] Source visibility `workspace` or `restricted`; grants to users or groups. Admin role does not implicitly read restricted content: an admin must grant themselves access, which is audited.
- [built] RLS on all tenant tables, fail closed when context is unset. App connects as a non-owner, non-superuser role.
- [built] Revocation takes effect at the next query; retrieval never caches ACLs.
- [built] Stored answers are withheld when a cited source is no longer visible.
- [planned] Document-level ACL and synced provider permissions (GitHub repo, Drive). Limitation: permissions today are source-level.

## 5. Prompt injection and model output

Layers for the current slice:

1. [built] **No capability**: the model has no tools. Worst case today is a misleading answer.
2. [built] **Permission filtering before context assembly**: restricted text cannot reach the prompt, so no injection can extract it.
3. [built] **Separation**: excerpts go in the user turn inside nonce-delimited blocks labelled as data. Delimiter-like text in documents is neutralized.
4. [built] **Output validation**: citation markers must exist in supplied evidence; otherwise removed and flagged. Uncited answers are downgraded.
5. [built] **Secret redaction** on text entering model context and logs (pattern-based, so not exhaustive).
6. [built] **UI safety**: answer text is rendered as text, no HTML or Markdown link rendering, so injected markup or exfiltration images cannot fire.
7. [built] Adversarial cases in `backend/evals` and `tests/security`.

Delimiting is a mitigation, not a boundary: a model can still be misled. That is
why layers 1, 2 and 4 do not depend on model behaviour, and why evaluation results
for injection are reported as measured, per model.

Later layers *(planned)*: tool allowlists per task; approval gates outside the
model; treating any tool output as tainted, so that a plan derived from untrusted
content cannot auto-execute writes; sandboxed code execution with no secrets and
no network by default.

## 6. Data egress

- [built] Default `LLM_PROVIDER=extractive` and `EMBEDDING_PROVIDER=hash`: nothing leaves the process. Hash embeddings have no semantic value; they exist for tests and offline use.
- [built] Ollama (local) is the recommended development setup. A non-loopback Ollama URL counts as external.
- [built] External providers refuse to start unless `EXTERNAL_MODEL_DATA_APPROVED=true`. This is the documented data-processing decision point; the organization must check vendor retention and region terms. Not automatically satisfied.
- [built] Audit records carry the provider and model that received the context.

## 7. Input handling

- [built] Upload allowlist (`.md`, `.markdown`, `.txt`, `.pdf`), size limit, magic-byte check for PDF, UTF-8 and no-NUL check for text, page limit and extraction cap for PDF, filename sanitization. Blob keys are server-generated, never derived from the filename.
- [built] Pydantic validation with unknown-field rejection on request bodies.
- [built] Parameterized SQL only.
- [planned] Malware scanning; sandboxed PDF parsing. Limitation: `pypdf` runs in the worker process; a parser bug is reachable by anyone with upload permission. Run workers with low privilege and no provider secrets.
- [planned] SSRF controls for any URL-fetching connector.

## 8. Secrets and logging

- [built] Settings from environment; `.env.example` has placeholders only; secret values use `SecretStr`.
- [built] Log processor redacts sensitive keys and token-like values. Document content and questions are not logged.
- [built] SQL bound parameters are hidden from database error messages, since they can hold document text.
- [built] Failed-ingestion messages shown to users are fixed text or describe the file. Internal errors stay on the job row.
- [planned] Encrypted credential storage with envelope encryption and rotation for OAuth tokens; or a secrets manager. No raw tokens in ordinary tables.

## 9. Audit, retention, compliance

- [built] `audit_events` is append-only for the app role at the database level (`INSERT, SELECT` only).
- [built] Audited: login success and failure, logout, source create and delete, grant changes, document upload and delete, knowledge query with source IDs, tool server changes, discovery, tool review, definition changes, and every tool invocation including denials.
- [planned] Retention policy, audit export, admin audit UI, backup and restore validation.

**No regulatory compliance is claimed.** GDPR, SOC 2, HIPAA and similar are open
decisions that depend on deployment and data. Encryption at rest and in transit
are deployment responsibilities and not yet configured in the local stack: the
local compose file uses plaintext connections and development-only passwords.

## 10. Tool and MCP controls

Nothing calls tools automatically yet: no model or workflow is connected to the gateway. Calls come
only from an authenticated person through the API.

- [built] Registration, discovery and review need `tools:admin`. Invocation needs `tools:invoke` (member and above), checked in the gateway as well as the route. Other workspaces get 404, and RLS covers all three tool tables.
- [built] Discovered tools start disabled at Level 3. Server annotations such as `readOnlyHint` are stored and displayed, but grant nothing.
- [built] Pinning: name, description, schema and annotations are hashed. Each call compares the live hash first; a mismatch means no call, the tool is disabled and audited, and the approved text stays in place. Review names the exact hash, and reverting a definition does not re-enable a tool.
- [built] **Only Level 0 read tools run.** Writes are refused until Phase 4, even if an admin approves them.
- [built] Arguments are validated against the pinned schema, with a size limit. Unknown top-level fields are always refused. Validation errors never echo values.
- [built] Schemas cannot trigger requests: non-local `$ref` is rejected at discovery, and validation runs with an empty reference registry. jsonschema's default would fetch remote references.
- [built] SSRF policy for server URLs: public HTTPS only, every DNS answer must be public, and no credentials, query or fragment. Checked at registration and before each connection. `TOOL_ALLOWED_PRIVATE_HOSTS` is the explicit exception.
- [built] Stdio commands come only from `MCP_STDIO_SERVERS` in deployment configuration. The API can only name one. Child processes get a minimal environment, not this process's secrets.
- [built] The MCP client declares no sampling, elicitation or roots capability and has response caching off.
- [built] Tool output is marked untrusted, secret-redacted and size-capped. Provider error text is never returned to callers.
- [built] Invocation log stores hashes, sizes and outcomes, never arguments or output, and the app role cannot delete it. Denied calls are recorded too.
- [built] Timeout, retries for reads, per-user rate limit, per-server circuit breaker. Limiter and breaker state are per process.

Known gaps:

1. **Admins decide what "read" means.** The platform cannot tell whether a tool really is read-only. An admin who classifies a write tool as Level 0 read makes it callable. Review is the control.
2. **DNS rebinding:** the policy check and the connection resolve the name separately. Use network-level egress rules in production.
3. **Hostile server memory use:** output limits apply after the response is received, and the SDK does not cap JSON response size. A hostile HTTP server could exhaust memory, so register only servers you trust that far.
4. **Hostile schemas:** a `pattern` can be slow to evaluate (ReDoS) during validation, and only the schema's total size is limited. Reviewers should reject unusual patterns.
5. No per-tool or per-user tool grants: any member can call any enabled read tool. Argument hashes are unsalted, so someone with the log could confirm a guessed value.
6. A crash mid-call leaves an invocation at `running`. It is visible but nothing reconciles it.
7. Stdio servers are a deployment-wide list, so any workspace admin can register a configured one.

Planned: per-server credentials in an encrypted store (next); scheduled re-discovery with alerts;
treating tool output as tainted in the orchestrator, so a plan derived from it cannot auto-execute.

## 11. Known gaps at this milestone

1. Login throttling and any rate limiting are per-process.
   Client IP is the direct peer: behind a proxy, configure trusted forwarded headers or IP throttling is meaningless.
2. No general API rate limiting beyond login and a per-user question limiter.
3. Conflict surfacing depends on the model noticing, except for what the eval harness measures.
4. Secret redaction is pattern-based.
5. No malware scanning of uploads.
6. Local blob store has no encryption at rest.
7. An approved local Ollama daemon's own security is out of scope.
