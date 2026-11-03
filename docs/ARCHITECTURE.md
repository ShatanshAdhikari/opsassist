# Architecture

Status: draft for review, 2026-11-03. Sections marked *(planned)* are not built.
Update 2026-11-04: the tool registry and gateway now exist in `tools/`, read-only, so section 3.2 is
partly built. See `INTEGRATION_STRATEGY.md` section 6 for what differs. Approvals are still planned.

## 1. Shape: modular monolith with separate worker process

One Python package, deployed as two processes from the same image: `api` and
`worker`. Modules talk through in-process interfaces. Services get extracted only
when scaling, isolation or ownership require it (ADR-0001). The first likely
extraction is the sandbox for code execution, for isolation reasons.

```
                        ┌────────────────── Browser ──────────────────┐
                        │ React + TS: Assistant, Knowledge, citations │
                        └───────────────────┬─────────────────────────┘
                     cookie session + Origin check │ HTTPS
 ═══════════════════ TRUST BOUNDARY: authn/authz ══╪══════════════════════════
                                                   ▼
 ┌─────────────────────────────── api process ───────────────────────────────┐
 │ Middleware: request ID, logging, CSRF origin check, error envelope        │
 │ Routes (thin) ──▶ RequestContext(user, workspace, role, RLS transaction)  │
 │                                                                           │
 │  identity      audit        knowledge            assistant                │
 │  sessions      append-only  sources, documents   retrieve ▶ assemble ▶    │
 │  roles                      access (ACL choke    generate ▶ validate      │
 │                             point), retrieval    citations                │
 │                                                                           │
 │  integrations/ tool registry, MCP gateway, policy, approvals (planned)    │
 └───────┬──────────────────────────┬───────────────────────────┬────────────┘
         │ SQL as non-owner role,   │ blobs                     │ data leaves only
         │ RLS enforced             ▼                           │ per config
         ▼                   ┌────────────┐      ══ TRUST BOUNDARY: model ══
 ┌───────────────────┐       │ BlobStore  │                     ▼
 │ PostgreSQL        │       │ local / S3 │      ┌──────────────────────────┐
 │ + pgvector        │       └─────▲──────┘      │ LLM and embedding        │
 │ tenancy, chunks,  │             │             │ providers: none, Ollama, │
 │ FTS, jobs, audit  │◀──┐         │             │ Anthropic                │
 └───────────────────┘   │         │             └──────────▲───────────────┘
                 ┌───────┴─────────┴─────────┐              │
                 │ worker process            │──embeddings──┘
                 │ SKIP LOCKED job queue,    │
                 │ leases, retry, dead state │
                 │ extract ▶ chunk ▶ embed   │
                 └───────────────────────────┘
```

## 2. Module responsibilities

| Module | Owns | Must not |
| --- | --- | --- |
| `api` | HTTP, validation, envelopes, dependency wiring | Hold business logic |
| `identity` | Users, sessions, memberships, permission map | Know about knowledge internals |
| `audit` | Append-only event writing | Be updatable by the app DB role |
| `jobs` | Durable queue and worker loop | Know job business logic beyond a handler registry |
| `knowledge` | Sources, documents, ingestion, chunking, retrieval, ACL | Call an LLM for generation |
| `llm` | Provider protocols and adapters, data-egress guard | Touch the database |
| `assistant` | Answer pipeline, prompt assembly, citation validation | Query chunks except through `knowledge.retrieval` |
| `evals` | Offline evaluation against fixtures | Be imported by runtime code |

Dependency direction: `api → assistant → knowledge → (db, llm protocols)`;
`identity` and `audit` are leaf services.

## 3. Key data flows

### 3.1 RAG question (built)

1. `POST /workspaces/{id}/conversations/{cid}/messages`. Session resolves the user; membership resolves the role; non-members get 404.
2. Transaction A, with `app.workspace_id` set for RLS: store the user message.
3. Embed query. On embedding failure, continue lexical-only and flag `dense_retrieval_unavailable`.
4. Transaction B: run dense and lexical queries against current versions, each constrained by one `visible_sources` SQL fragment (workspace and user or group grants). HNSW iterative scan prevents filtered queries from under-returning.
5. Reciprocal rank fusion, dedup, relevance gate, context budget.
6. No evidence: return "no reliable source" and **do not call the model**.
7. Outside any DB transaction: redact secrets, wrap excerpts in nonce-delimited data blocks, call the provider. Model has no tools.
8. Validate: citation markers must match supplied evidence IDs; unknown markers are removed and reported; uncited answers are downgraded and the evidence is shown instead.
9. Transaction C: store answer, citation snapshot, usage record, audit event.
10. Reads of old messages re-check citation access; if a cited source is no longer visible, content is withheld.

### 3.2 MCP tool call *(planned, Phase 3)*

```
orchestrator ─▶ gateway: tool_id + args + actor
   gateway: registry lookup (enabled? healthy? schema pinned?)
   gateway: validate args against pinned JSON schema, reject unknown fields
   gateway: policy check (workspace, role, connection scopes, risk level)
   gateway: Level 0 → continue; Level 1+ → approval flow (3.3)
   gateway: resolve credential reference, call with timeout/rate limit/breaker
   gateway: validate and size-limit output; mark as untrusted; redact secrets
   gateway: record tool_invocation (args hash, outcome, latency)
```

MCP tool descriptions are stored and pinned at registration. A changed schema or
description disables the tool until an admin reapproves it. Descriptions shown to
a model are admin-reviewed text, not whatever the server currently advertises.

### 3.3 Approval-gated cross-system workflow *(planned, Phases 4 and 5)*

Email to GitHub issue:

```
read thread (L0) ─▶ extract fields ─▶ search related issues (L0) ─▶ retrieve docs
   ─▶ prepare ProposedAction[create_issue, send_reply] with exact content + evidence
   ─▶ persist state: awaiting_approval ── worker may stop here; state survives restart
   ─▶ human approves, rejects or edits, per action, in approval inbox
   ─▶ policy re-check at execution time (permissions may have changed)
   ─▶ create_issue with idempotency key ─▶ verify via read-back
   ─▶ send_reply: if outcome uncertain, reconcile by searching Sent; never blind retry
   ─▶ audit every step; report partial completion honestly
```

Approval binds to a hash of the exact payload; any edit requires a new approval
record. The LLM can propose actions but cannot create or satisfy an approval.

## 4. Trust boundaries

1. Browser to API: nothing from the client is trusted. Session cookie plus Origin check.
2. API to database: app connects as a non-owner role subject to RLS, with no update or delete on audit.
3. Application to model provider: only assembled, permission-filtered, redacted context crosses. External providers need an explicit flag.
4. External content to application: documents, emails and tool output are data. They are never placed in the instruction role.
5. Platform to MCP servers: servers are untrusted capability providers (`SECURITY_MODEL.md`).

## 5. Data model

Built: `organizations, users, workspaces, memberships, groups, group_members,
sessions, audit_events, knowledge_sources, source_grants, documents,
document_versions, chunks, jobs, conversations, messages, usage_records`.

Planned: `integration_connections, credential_refs, mcp_servers,
tool_definitions, tool_invocations, workflows, workflow_steps, approval_requests,
policies`.

Conventions: UUID primary keys, `workspace_id` denormalized on every tenant table
for RLS and index locality, timestamps in UTC, migrations hand-reviewed in Alembic.

## 6. Tenancy enforcement layers

1. Route: `RequestContext` dependency verifies membership.
2. Service: role permission check.
3. SQL: every knowledge read goes through `knowledge/access.py`.
4. Database: RLS policies on `app.workspace_id`, fail closed when unset. App role is neither owner nor superuser.

User and group restrictions within a workspace (layer 3) are application SQL, not
RLS, because the worker has no user. Cross-tenant and cross-user tests exercise
both (`tests/security`).

## 7. Reliability

Jobs: `FOR UPDATE SKIP LOCKED`, lease with heartbeat, exponential backoff with
jitter, `dead` state after max attempts. Ingestion is idempotent per version:
chunk replacement and version activation happen in a single transaction.

Model calls happen outside DB transactions so slow generation never pins a
connection.

Degradation matrix:

| Failure | Behaviour |
| --- | --- |
| Embedding provider down at query | Lexical only, answer flagged |
| Embedding provider down at ingest | Job retries with backoff, then `dead`, visible in UI |
| Model down or timed out | Return retrieved excerpts, flagged `model_unavailable`, not presented as a generated answer |
| Database down | `/readyz` 503; API returns 503 envelope; no answer |

## 8. Extension points

- New document type: `Extractor` plus `Chunker`, registered by content type.
- New retrieval stage: reranker hook between fusion and budget *(planned)*.
- New model vendor: implement `LLMProvider` / `EmbeddingProvider`.
- New integration: provider adapter or MCP server behind the gateway contract (`INTEGRATION_STRATEGY.md`).
- New job type: register a handler by `kind`.

## 9. Technology

See ADRs: 0001 monolith, 0002 Postgres + pgvector, 0003 Postgres job queue,
0004 RLS, 0005 model providers and egress, 0006 sessions, 0007 frontend.
