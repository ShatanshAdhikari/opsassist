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

## ADR-0011: Envelope-encrypted credentials in PostgreSQL

**Decision.** AES-256-GCM per secret, with data keys wrapped by a key-encryption key from deployment
configuration. Behind a `CredentialStore` class.

**Alternatives.**
- Secrets manager or KMS (Vault, AWS KMS): stronger, with audit and access control on the key
  itself, but needs infrastructure the deployment has not chosen. This is the production direction.
- pgcrypto: the key would pass through SQL, where it can reach logs and statements.
- Single encryption without envelopes: rotation would have to re-encrypt every secret.

**Consequences.** Works with no new infrastructure, and credentials share RLS and transactions with
their connection. The key is an environment variable, so the protection is against database-only
exposure, not against someone who also has the process environment. Losing the key means re-entering
credentials, so it needs its own backup.

## ADR-0014: A mailbox belongs to one person, enforced outside the model

**Decision.** Connections and tool servers gain an optional `owner_user_id`. When set, only that person
can call the tools, see them offered to the model, or read stored answers citing them. Admins get no
exception: they can approve tools and remove the connection, which revokes it first. Gmail always has
an owner, enforced by a database constraint. The foreign key cascades, because a null owner means
shared, so losing an owner must not expose a mailbox.

**Alternatives.**
- Workspace-wide like GitHub (ADR-0012): simple, but one person's private mail would be readable by
  every member. Rejected outright.
- A separate tool registry per user: clean isolation, but duplicates review and audit machinery that
  should be shared.
- Admin-created connections: fits the existing routes, but a mailbox can only be authorized by its
  owner, so members connect their own with `tools:invoke`.

**Consequences.** Three places must honour ownership: the gateway, the catalog and stored answers. All
are tested, and `usable_by` holds the rule. Tools still need an admin's approval, so a member's mailbox
is unusable until an admin acts. Search returns whole threads rather than IDs, because the planner
cannot chain calls (ADR-0013). That costs more per search. Sending mail to an external model needs
`EXTERNAL_MODEL_MAIL_APPROVED`, on top of the general egress approval.

## ADR-0013: Plan once, before reading any untrusted content

**Decision.** A question runs as a fixed sequence: one planning call, the planned read-only tool
calls, document retrieval, one answering call. The planning prompt holds only the user's questions
and the catalog of admin-approved tools. Tool output, documents and earlier answers never reach it.
The answering call has no tools. Limits cover calls, time and tokens per question.

**Alternatives.**
- A loop where the model reads each result and picks the next call: handles chains such as "list my
  PRs, then read each one", but hostile text in an issue could then choose the next call and place
  data in its arguments, reaching a third-party server. Read-only tools do not prevent that.
- Loop with provenance checks on arguments: promising, but needs typed values from tool output and
  real usage data to design well. The likely next step when chains are needed.
- Native provider tool calling: better on some vendors, but llama3 through Ollama lacks it, and
  the Anthropic adapter has never run live. JSON text keeps one code path, and is validated anyway.

**Consequences.** Injected tool content cannot select a tool or set an argument, whatever the model
does, because planning has finished before it is read. **One result cannot feed another call**, so
"review this issue and inspect the relevant code" needs the user to name the file. Content pasted
into a question is the user's own and can steer planning, but only to calls they could make anyway.
Not durable: it runs in the request, which is acceptable only while every call is a read. Phase 4
must revisit this, with Temporal (ADR-0003).

Per-call MCP cost was measured first, as ADR-0009 asked: on loopback against the mock server, HTTP
p50 13 ms and stdio p50 1.3 s (Windows, process start included), which is small beside model latency.
Connect-per-call stands, with a real server's network distance still unmeasured.

## ADR-0012: GitHub access is a workspace decision for now

**Decision.** Use a GitHub App with an admin-chosen repository list per connection. Any member with
`tools:invoke` can read those repositories. Do not check each person's own GitHub access yet.

**Alternatives.**
- Linked per-user OAuth, checking access on every call: correct for repositories with different
  audiences, but it needs an OAuth flow and token refresh, and users without linked accounts get
  nothing. The original design called for this before shipping.
- Personal access tokens: simple, but tied to one person, with long-lived broad access.

**Consequences.** This departs from the original design and is a real limit: a member can read a
private repository they could not open on GitHub, if an admin put it on the connection. It is
documented in `GITHUB_SETUP.md`. **Add per-person checks before any workspace needs repositories
with different audiences.** This ADR needs the owner's explicit agreement.
