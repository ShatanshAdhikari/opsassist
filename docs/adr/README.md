# Architecture Decision Records

Status of all: **proposed, pending review by the project owner**, 2026-11-03.
They were accepted provisionally to allow implementation to proceed.

## ADR-0001: Modular monolith, separate worker process

**Decision.** One Python package, deployed as `api` and `worker` from one image.

**Alternatives.** Microservices from day one: independent scaling, but distributed
transactions, network failure modes and deployment overhead, with one team and no load
data. Rejected for now.

**Consequences.** Module boundaries are enforced by convention and review, not the
network. Likely first extraction: code-execution sandbox.

## ADR-0002: PostgreSQL with pgvector for relational, vector and lexical retrieval

**Decision.** Postgres 17 with pgvector HNSW for dense search and built-in full-text
search for lexical.

**Alternatives.**
- Dedicated vector DB (Qdrant, Weaviate): better at very large scale and filtered ANN. But ACLs and tenancy must be duplicated and kept in sync with the relational source, and deletes become eventual. Revocation consistency is a core requirement here.
- OpenSearch: strongest lexical, heavy to operate.

**Consequences.** Permission filter and chunks share a transaction and RLS, so revocation
is immediate. Filtered ANN needs `hnsw.iterative_scan` (pgvector 0.8+). Embedding
dimension is fixed in the column (768); changing model dimension needs a migration and
re-embedding. Lexical quality is below BM25. Revisit at roughly 10 million chunks, or if
evaluation shows lexical recall is the limit. Retrieval sits behind a module boundary.

## ADR-0003: Postgres-backed job queue for now

**Decision.** `jobs` table with `FOR UPDATE SKIP LOCKED`, leases, backoff, dead state.

**Alternatives.**
- Celery or RQ with Redis: mature, but adds a broker, and enqueue is not transactional with the data, requiring an outbox anyway.
- Temporal: excellent for durable workflows and likely worth evaluating for Phase 4, but heavy for ingestion alone.

**Consequences.** Enqueue commits atomically with the document row. No Redis needed yet.
Throughput is bounded by Postgres; fine for ingestion. **Phase 4 must re-evaluate Temporal
against a workflow table on this queue before approval workflows are built.**

## ADR-0004: Row-level security as defence in depth

**Decision.** RLS on all tenant tables keyed on `app.workspace_id`, set with transaction
scope. App connects as `opsassist_app`, non-owner and non-superuser. Migrations use the
owner role.

**Alternatives.** Application filters only: simpler, but one missed `WHERE` is a tenant
leak. Separate database per tenant: strong, but heavy for many workspaces.

**Consequences.** Two DB roles. Transaction-scoped context is compatible with PgBouncer
transaction pooling. Cross-tenant admin work needs an explicit privileged path. User and
group ACLs remain in application SQL, since the worker has no user.

## ADR-0005: Model provider abstraction with default-deny data egress

**Decision.** `LLMProvider` and `EmbeddingProvider` protocols. Adapters: `extractive`
(no model, returns excerpts), `hash` (deterministic test embedding), `ollama` (local),
`anthropic` (external). External adapters refuse to load without
`EXTERNAL_MODEL_DATA_APPROVED=true`.

**Alternatives.** LiteLLM or LangChain: many vendors at once, but a large dependency and
abstraction surface for two small HTTP calls, and harder to audit egress. Rejected until
vendor count justifies it.

**Consequences.** No agent framework. Prompt assembly and validation are explicit code.
Each new vendor is a small adapter.

## ADR-0006: Server-side opaque sessions in an HttpOnly cookie

**Decision.** Random token, SHA-256 stored, cookie transport, Origin check for CSRF.

**Alternatives.** JWT: stateless, but revocation needs a denylist anyway, and browser
storage invites XSS theft. Rejected. OIDC later plugs into the same session creation.

**Consequences.** One DB read per request; acceptable.

## ADR-0007: React, TypeScript and Vite; no rich-text rendering of model output

**Decision.** SPA with hand-written CSS tokens and a small dependency set. Model output
renders as plain text with citation chips parsed from validated markers.

**Alternatives.** Next.js: SSR is unnecessary behind login. Component kits: faster, but
more dependency weight. Markdown rendering: nicer, but it opens image-based exfiltration
and link injection from model output; revisit with a strict sanitizer and no remote images.

**Consequences.** Answers look plainer.

## ADR-0008: Python toolchain

**Decision.** Python with FastAPI, SQLAlchemy 2 async, asyncpg, Alembic; `uv` for locked
dependencies; ruff and mypy.

**Alternatives.** TypeScript backend: unified language, but weaker document-processing and
eval ecosystem. Python's MCP SDK is official and mature.

**Consequences.** Async discipline needed; CPU-heavy parsing runs in the worker via threads.

## ADR-0009: Official MCP SDK 2.x, connecting per operation

**Decision.** Use the official `mcp` package, pinned `>=2.3,<3`, through its high-level `Client`.
Open a connection for each discovery or invocation, and list tools before every call to compare
the pinned definition hash.

**Alternatives.**
- `mcp` 1.x: more examples exist, but new work on an old major version means a migration later.
- Long-lived pooled sessions: faster, but they need reconnection and state handling, and a cached
  tool list would weaken the definition check.
- Periodic re-discovery only: cheaper per call, but leaves a window in which a changed tool runs.

**Consequences.** Every call costs a handshake and a tool listing, and a stdio call also costs a
process start. Per-call latency has not been measured on its own. This should suit human-paced
reads but may not suit a planner making many calls, so measure before building one.
SDK 2.x is recent, so upgrades need a review of the changelog.

## ADR-0010: Tool layer ships read-only, with writes refused in the gateway

**Decision.** `not_callable_reason` refuses anything other than `access=read` at risk level 0,
regardless of admin classification, until the approval flow exists.

**Alternatives.** Trust admin classification now: faster to demo, but it would let a consequential
action run with no approval step, which the brief forbids.

**Consequences.** Phase 4 has one place to change. Admin classification of a tool as read remains a
trust decision: see `SECURITY_MODEL.md` section 10.
