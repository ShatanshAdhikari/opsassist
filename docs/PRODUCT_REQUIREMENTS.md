# Product Requirements

Status: draft for review, 2026-11-03. Source brief: `CLAUDE.md`.

## 1. Product

A centralized AI operations assistant for one organization and its workspaces.
Users ask in natural language; the platform answers from approved knowledge,
reads live systems through controlled tools, prepares work for review, and
executes consequential actions only after explicit approval.

It is an **auditable operational system**, not a chatbot wired to Gmail and
GitHub. Permissions, approvals and audit live in conventional software, outside
the LLM.

## 2. Users and problems

| User | Problem solved |
| --- | --- |
| Engineer | PR/issue state, code context, duplicate bugs, digests, without opening every tool |
| Support / customer-facing staff | Drafts grounded in approved documentation |
| On-call / incident lead | Runbook and owner lookup, incident timeline assembly |
| Manager / lead | Status, risk and release-readiness reports with sources |
| Workspace admin | Control over integrations, scopes, approval rules, audit, spend |

## 3. Functional requirements

Priorities: **P0** first release (Phases 1 and 2), **P1** tools and actions
(Phases 3 and 4), **P2** cross-system workflows and hardening (Phases 5 and 6).

### Identity and tenancy
- FR-1 (P0) Password login with server-side sessions; logout revokes. OIDC is P1.
- FR-2 (P0) Organization > workspace > membership hierarchy with roles.
- FR-3 (P0) Non-members get 404, not 403, for workspace resources.
- FR-4 (P0) Audit events for authentication, access changes, all writes, and knowledge queries.

### Knowledge (RAG)
- FR-10 (P0) Register a knowledge source with `workspace` or `restricted` visibility.
- FR-11 (P0) Upload Markdown, text and PDF with type, size and content validation.
- FR-12 (P0) Durable asynchronous ingestion with retry, dead-lettering and visible status.
- FR-13 (P0) Structure-aware chunking: Markdown headings and tables, text paragraphs, PDF pages.
- FR-14 (P0) Hybrid retrieval: dense, lexical, permission filters, rank fusion.
- FR-15 (P0) Document versioning, content-hash dedup, deletion that removes chunks.
- FR-16 (P0) Answers cite sources with section, page or line; invented citations are removed and flagged.
- FR-17 (P0) State "no reliable source" instead of answering without evidence.
- FR-18 (P0) Conflicting sources surfaced; stale sources marked.
- FR-19 (P0) Repeatable evaluation harness with retrieval, citation and permission metrics.
- FR-20 (P1) Office documents, documentation portals, repository docs, selected email threads.
- FR-21 (P1) Reranking and query decomposition, only where evaluation shows a benefit.

### Tools and MCP (Phase 3)
- FR-30 Tool registry with stable IDs, provider identity, JSON schema, risk level, scopes, timeouts, idempotency, health.
- FR-31 MCP client with discovery, capability-change detection, disconnect handling.
- FR-32 Schema validation and permission checks before every invocation; every invocation recorded.
- FR-33 Read-only GitHub and read-only email integrations with minimal scopes.

### Controlled actions (Phase 4)
- FR-40 Read / prepare / execute separation; risk levels 0 to 3.
- FR-41 Approval inbox: exact content, target, reason, evidence, risk; approve, reject, edit.
- FR-42 Policy engine with safe defaults and organization configuration.
- FR-43 Idempotent execution, reconciliation of uncertain writes, verification of outcomes.

### Workflows (Phase 5)
- FR-50 Email to issue draft; PR review with documentation; weekly digest; project status; incident summary.
- FR-51 Each has explicit inputs, outputs, permissions, failure handling and acceptance tests.

## 4. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Security | Authorization enforced before content reaches a model. Tenant isolation enforced by the database as well as the application. |
| Privacy | No organizational data sent to an external model provider without an explicit configuration decision. Default is local-only. |
| Reliability | Ingestion survives worker crash. Workflows resume after restart. Non-idempotent writes never blindly retried. |
| Degradation | Embedding, model or integration outage is reported as such; never implied to have succeeded. |
| Observability | Structured logs with correlation IDs, audit trail, per-request model tokens and latency. Metrics and traces in Phase 6. |
| Operability | Documented install, run and test from clean checkout. Migrations run from an empty database. |
| Cost | Configurable limits on retrieved context, tokens, and later spend per user and workspace. |
| Accessibility | Keyboard-usable, labelled controls, works on desktop and mobile widths. |

Latency and scale targets are deliberately unset until there are real workloads.
Provisional target for evaluation: p95 retrieval under 500 ms on 100k chunks.

## 5. Out of scope for the first release

- Real external integrations (Phase 3 onward).
- Any write action on an external system.
- Autonomous agents and model tool calling. The first slice gives the model no tools at all.
- Claims of regulatory compliance. See `SECURITY_MODEL.md` section 9.

## 6. Success criteria

Taken from brief section 18. An authorized employee can ask across approved
sources and get a cited answer; understand email, GitHub and project state;
convert between systems as drafts; delegate bounded workflows; approve
consequential actions; trace any answer or action; and an integration can be
added without redesign, with permissions enforced outside the LLM.
