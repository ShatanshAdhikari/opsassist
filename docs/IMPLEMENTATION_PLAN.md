# Implementation Plan

Last updated: 2026-11-03. Status reflects verified state, not intent.

## Assumptions (recorded so they can be challenged)

1. Greenfield. Repository held only `CLAUDE.md`.
2. One organization at first, but schema and enforcement are multi-tenant.
3. No external model API keys available; default must work with no data egress. Local Ollama is present for real embeddings and generation.
4. Target deployment is undecided; Docker image and compose are the portable baseline.
5. Project name `opsassist` is a placeholder.

## Open decisions (none block Phases 0 to 2)

| # | Decision | Needed by | Default if unanswered |
| --- | --- | --- | --- |
| 1 | Which external model vendor is approved for org data, if any | Production | Local only |
| 2 | Google Workspace internal app or public Gmail OAuth (restricted-scope review) | Phase 3 email | Internal Workspace app, sandbox account |
| 3 | GitHub org and sandbox repo for the App install | Phase 3 GitHub | Mock server only |
| 4 | SSO provider | Before real users | Password login |
| 5 | Compliance regime | Before production data | None claimed |
| 6 | Retention periods | Phase 6 | Keep until deleted |

## Phases

### Phase 0: Discovery and architecture. Written, pending owner review.
Docs in `docs/`, ADRs in `docs/adr/`.

### Phase 1: Foundation. Implemented. CI defined but never run (no remote).
Project structure, compose, migrations, auth, workspace model, logging, error
envelope, health checks, CI.

Acceptance: new developer runs from docs; migrations work from clean DB; auth and
workspace boundaries tested; CI runs lint, types, tests.

### Phase 2: Cited document Q&A (first vertical slice). Implemented, pending owner review.
Source registration, upload, durable ingestion, structure-aware chunking, embeddings,
hybrid retrieval, permission filtering, citations, conversation UI, eval harness.

Acceptance:
- Upload Markdown and PDF, see status, ask, receive answer with section, page or line citation.
- User without grant gets no restricted content, proven at retrieval and answer level.
- Cross-workspace leak tests pass with RLS on and with the application filter bypassed.
- Invented citations are stripped. No evidence means no model call.
- Worker crash mid-job recovers.
- Model or embedding outage degrades honestly.
- Eval harness produces numbers for retrieval, citation and permission metrics.

### Phase 3: MCP and integration foundation. Tool layer implemented, connectors not started.
Tool registry, MCP client, schema validation, permission checks, invocation log,
timeouts, mock MCP server. Then read-only GitHub, then read-only email.

Agreed order after the tool layer: credential store and GitHub, conversation context and planner,
Gmail, Calendar, scheduled tracking. Calendar and tracking were added at the owner's request.

Acceptance: unknown fields rejected; disabled or changed tools not callable; outage
reported; invocation audited; injected tool output cannot trigger any action; live PR
status answer cites API with timestamp.

Status of those criteria: all met against a mock server, except the last, which needs GitHub. The
injection criterion holds trivially for now, since no model or workflow consumes tool output. It
must be retested when the planner arrives.

### Phase 4: Controlled actions
Workflow state machine, approval inbox, policy engine, idempotent execution, read-back
verification, audit UI. Start with Level 1 (Gmail draft, issue draft).

Acceptance: action refused without approval; edit invalidates approval; duplicate
execution blocked; workflow resumes after restart; uncertain write reconciled.

### Phase 5: Cross-system workflows
Email to issue draft; PR review with RAG; weekly digest; status report; incident summary.

Acceptance: each has inputs, outputs, permissions, failure handling, acceptance test,
and honest partial-completion output.

### Phase 6: Production hardening
Security review, load tests, backup and restore validation, metrics and alerts, cost
budgets per user and workspace, deployment automation, runbooks, admin UI.

## Progress log

See `docs/PROGRESS.md` for what has been verified at each step.
