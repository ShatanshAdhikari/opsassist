# Progress Log

Records what was verified, not what was intended. Newest first.

## 2026-11-04: Credentials and read-only GitHub, branch `feat/github-readonly`

Stacked on `feat/tool-layer`.

### What changed

- `credentials/`: envelope-encrypted secrets, key rotation (ADR-0011).
- `integrations/`: connection lifecycle, GitHub App authentication, native read-only provider with
  five tools. Access decision in ADR-0012, which needs the owner's agreement.
- Migration `0003`: `integration_connections`, `stored_credentials`, GitHub as a tool server kind.
- API under `/connections`. CLI: `generate-credential-key`, `rotate-credential-keys`.
- Setup and revocation steps in `GITHUB_SETUP.md`.

### Verified results

| Check | Result |
| --- | --- |
| PR #1 CI (`feat/tool-layer`), first passing CI, includes the eval step | backend and web passed |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (65 source files) |
| Full backend suite, Windows, Python 3.14 | 308 passed, 2 live tests deselected |
| Full backend suite, Linux container, Python 3.13, direct Docker network | 308 passed |
| Test database: up, down to `0002`, up, down to base, up, `alembic check` | passed, no drift |

### Not verified

- **Real GitHub has not been called.** Every result above is against a fake written from GitHub's
  documentation. The fake checks JWT signatures and token scoping, but it cannot show that GitHub
  behaves as documented. `tests/integration/test_github_live.py` covers this once an app exists,
  and has not been run.
- That the down-scoped token cannot write. Tested only in that the request asks for read-only.
- `opsassist rotate-credential-keys` moving real rows as a command. It ran with nothing to move, and
  the rotation logic underneath it is tested separately.
- Migration `0003` was not applied to the development database.

### Known limitations

- No per-person GitHub access check (ADR-0012).
- API only. No screen, and the assistant does not use these tools yet.
- Deleting a connection does not revoke anything on GitHub.
- Encryption keys are environment variables.
- One owner per connection, and no pagination beyond the first page of each result.

## 2026-11-04: Tool layer (Phase 3, first step), branch `feat/tool-layer`

### What changed

- `backend/src/opsassist/tools/`: tool contract, registry with admin review and definition pinning,
  gateway enforcing policy, schema validation, SSRF policy, retry and circuit breaker, MCP provider
  on the official SDK over HTTP and stdio.
- Migration `0002`: `tool_servers`, `tool_definitions`, `tool_invocations`, all under RLS.
- API under `/api/v1/workspaces/{id}`: `tool-servers`, `tools`, `tools/{id}/review`,
  `tools/{id}/invoke`, `tool-invocations`.
- Bug found by the first CI run on `main`, fixed here: a database outage on Linux returned 500, not
  503, because asyncio raises plain `OSError` for a host with several addresses. Windows hid it.

Design reasoning is in ADR-0009 and ADR-0010, controls and gaps in `SECURITY_MODEL.md` section 10.

### Verified results

| Check | Result |
| --- | --- |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (57 source files) |
| Full backend suite, Windows, Python 3.14 | 221 passed |
| Full backend suite, Linux container, Python 3.13, direct Docker network | 221 passed |
| `tests/integration/test_tools.py`, same setup, 6 repeated runs | 40 passed each |
| Real MCP over stdio subprocess and over HTTP, including killing the HTTP server | passed |
| Test database: down to `0001`, up, down to base, up, `alembic check` | passed, no drift |
| First CI run on `main` (before this branch) | web passed; backend failed on the bug above, 139 of 140 |

### Not verified

- **CI has not run on this branch**, so the outage fix is unconfirmed under CI itself. The eval step
  has also never run in CI, since it comes after the failing test step.
- **Intermittent failure, not root-caused.** With the container reaching the database through
  `host.docker.internal`, 2 of 5 runs failed: a 60-second hang connecting to the database, at
  different tests each time. It did not occur in 7 runs over the direct Docker network. That points
  at Docker Desktop port forwarding rather than this code, but a 400-connection probe through a
  host-gateway route did not reproduce it, so this is likely, not proven.
- **No real integration or real third-party MCP server.** Only the mock in `tests/`, on the SDK.
- Migration `0002` was not applied to the development database.
- Latency under load. Each call costs a connection and a tool listing.
- Python versions differ: the image uses 3.13, CI and the local environment use 3.14. Both have
  been run, but nothing pins them together.

### Known limitations

- Backend API only. No Integrations screen yet.
- Nothing calls tools automatically: no planner, and the assistant does not use them yet.
- No credentials, so only servers needing no authentication can be registered.
- No scheduled re-discovery or alerts.
- Gaps specific to security are listed in `SECURITY_MODEL.md` section 10.

### Upgrading a running checkout

Stop the API and worker first, because Windows locks the virtualenv. Then
`uv sync --extra anthropic` and `uv run alembic upgrade head`, and restart.

### Recommended next step

Encrypted credential store, then read-only GitHub.

## 2026-11-03: Phases 0 to 2 implemented, pending owner review

The project owner was away and asked for work to continue, so implementation went ahead on the
provisional ADRs without the architecture review the brief asks for. Treat the ADRs as open.
Nothing has been committed: this folder is not a git repository yet.

### What exists

- **Phase 0:** requirements, architecture, security model, integration strategy, plan, ADRs.
- **Phase 1:** FastAPI app, PostgreSQL with RLS and two DB roles, Alembic migration, password login
  with server-side sessions, organizations, workspaces, roles, groups, audit trail, structured
  logs, error envelope, health checks, compose file, Dockerfile, CI definition.
- **Phase 2:** knowledge sources and grants; Markdown, text and PDF upload; durable ingestion;
  structure-aware chunking; hybrid retrieval; permission filtering; cited answers with validation;
  conversation and knowledge UI; evaluation harness.

### Verified results

| Check | Result |
| --- | --- |
| Backend tests (`pytest`), real PostgreSQL, non-owner role | 140 passed |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (47 source files) |
| Web `eslint`, `tsc`, `vitest` (15 tests), `vite build` | clean |
| Browser E2E: log in, create source, upload, wait for indexing, ask with llama3, inspect citation, check 390px width has no page overflow | 1 passed |
| Screens inspected by eye: answer view, knowledge view, mobile citation panel | looked correct |
| Clean database: fresh volume, `upgrade`, `downgrade base`, `upgrade`, `alembic check` | passed, no drift |
| `docker compose --profile app`: image builds, migration job exits 0, API `/readyz` ready, worker starts, runs as non-root | passed. No request flow was run through the containers |

### RAG evaluation

20 synthetic cases in `backend/evals/corpus.py` on a corpus of 10 documents. **This corpus is small
and the same one used while building, so these numbers show the pipeline works. They do not predict
quality on real documents.** Thresholds have not been tuned on separate data.

| Metric | Hash embeddings (CI) | nomic-embed-text + llama3 |
| --- | --- | --- |
| Retrieval recall (any expected document) | 0.714 | 1.0 |
| Both conflicting documents retrieved | 1.0 | 1.0 |
| Expected text present in evidence | 0.727 | 1.0 |
| Mean precision of retrieved set | 1.0 | 0.881 |
| Missing-source questions with false hits | 0 | 0 |
| Permission leaks | 0 | 0 |
| Expected source cited in answer | not run | 1.0 (14 of 14 answers) |
| Invented citations removed | not run | 0 |
| Faithfulness (lexical heuristic, not proof) | not run | 0.782 |
| Answer status | not run | 9 grounded, 5 partly cited, 6 no evidence |
| Injection case: model followed the instruction | not run | 0 of 1 |
| Retrieval latency p50 (local embedding included) | 18 ms | 559 ms |
| Mean answer latency, warm model | not run | 2.3 s |

Observations:

- llama3 surfaced both conflicting sources in both conflict cases, in this run. Conflict handling
  rests on model behaviour, so it needs measuring per model and on more cases.
- A single injection case against one model proves little. What does hold regardless of the model
  is in `tests/security`: a scripted "fully compromised" model still cannot reach restricted text
  or take any action.
- An earlier run returned `model_unavailable` once, on the first request while the model was
  loading. The system labelled it correctly and returned excerpts, but the cause was not captured
  (the log level hid it and the error omitted the status). The error now carries the HTTP status.
  It did not recur in the next run.
- Answer cost is not tracked in money. Tokens and latency are recorded per answer.

### Bugs the checks found

- Redelivered ingestion job for the active version deleted that version's chunks. Fixed, with test.
- Citations after the full stop ("Claim. [S1]") were counted as uncited. Found by the evaluation.
- Argon2 verification ran on the event loop. Moved to a thread.
- Internal error text was exposed on failed documents. Users now get a safe message.
- The citation-validity metric was meaningless because the server already strips invalid markers.
  Replaced by invented-citation count and expected-source-cited.

### Not verified

- **CI has never run.** The workflow exists, but there is no remote. Its commands were run locally
  on Windows, not on Linux, except the Docker build.
- **The Anthropic adapter has never called the real API.** No key was available. It is tested
  against a fake client only. It enables server-side refusal fallback by default where supported.
- Performance under load, or with more than a handful of documents. The HNSW index plan was not
  examined at scale.
- Backup and restore.

### Known limitations

- Grant and group management is API only. The Knowledge screen can create restricted sources, but
  cannot yet share them. Non-admin creators cannot grant access to their own source.
- Member, audit and admin screens do not exist yet, though their APIs mostly do.
- No user registration or password reset. Accounts come from the CLI.
- Permissions are per source, not per document.
- Each question is independent, with no follow-up context (deliberate, see `assistant/service.py`).
- Answers are not streamed, so long generations show only a waiting state.
- No reranking or query decomposition. Add only if evaluation on real data calls for it.
- Bulk re-indexing does not exist. Changing embedding model needs re-uploads.
- Orphan blobs from failed commits or failed deletes are logged at most, with no sweeper.
- Rate limits are per process.
- No metrics or tracing yet, only logs and audit.
- `jobs` has no admin view. Dead jobs are visible on the document and in the table.

### Security implications

See `SECURITY_MODEL.md`, which marks each control built or planned. Most important for reviewers:
the app role must stay non-owner and non-superuser or RLS stops applying, and
`knowledge/access.py` must remain the only definition of who can read a source.

### Recommended next step

Owner reviews the architecture and ADRs, then answers the open decisions in
`IMPLEMENTATION_PLAN.md`. Then Phase 3: tool registry, MCP client and mock server, followed by
read-only GitHub.
