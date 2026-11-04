# Progress Log

Records what was verified, not what was intended. Newest first.

## 2026-11-04: Read-only Gmail, branch `feat/planner`

### What changed

- `integrations/gmail_client.py`, `gmail_provider.py`, `gmail_connections.py`: Google sign-in with
  PKCE and a single-use state, token refresh and revocation, and two read-only tools.
- **Connections can now belong to one person** (ADR-0014). Migration `0004` adds `owner_user_id`, Gmail
  as a server kind, and `oauth_states`.
- Integrations screen: connect, reconnect, check, disconnect, and approve tools.
- Setup steps are in `GMAIL_SETUP.md`.

### Verified results

| Check | Result |
| --- | --- |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (71 source files) |
| Full backend suite, Linux container, Python 3.13, direct Docker network | 412 passed, 6 live tests deselected |
| `tests/integration/test_gmail.py`, fake Google at the HTTP level | 36 passed, included above |
| Test database: up, down to `0003`, up, `alembic check` | passed, no drift |
| RAG evaluation with hash embeddings, recall and leak gates | passed |
| Web component tests including the Integrations screen, `vite build` | 29 passed, clean |
| Full backend suite, Windows, Python 3.14 | not run on the final code |

Covered: only `gmail.readonly` is requested and any other grant is refused and given back; state is
single-use and bound to the person; the token is encrypted and appears in no response; expiry reports
`reauthorization_required` and reconnecting fixes it; revocation failure keeps the connection unless
forced; another member and a workspace owner both get 404 on someone's mailbox; injected email text
never reaches planning; an external model is not offered mail without its own approval; stored
answers are withheld after disconnecting.

### Not verified

- **Real Gmail has not been called.** Every result is against a fake written from Google's
  documentation. It needs the OAuth client in `GMAIL_SETUP.md`, then `test_gmail_live.py`.
- The Integrations screen in a browser, and the real Google consent flow through it.
- That Gmail refuses a send from this token. The live test checks this. Its assumption, that scope is
  checked before the body, is unproven, and the same assumption about GitHub turned out wrong.
- How real HTML email parses, or how well llama3 handles real threads.

### Known limitations

- **Weekly sign-in:** refresh tokens last 7 days for an app in Testing mode.
- Search reads up to 10 whole threads per call, and each call shares the 16,000-character tool context,
  so large results are cut and marked incomplete.
- `awaiting_reply` only says who wrote last. It cannot tell whether a reply is really needed.
- Text hidden in HTML, such as with `display:none`, is kept. It reaches only the toolless answer step.
- Admins can see a mailbox's address in `/connections`, though not its contents.
- Access token cache is per process, so several workers refresh separately. That is harmless.
- Screens for GitHub connections and other tools are still missing.

### Security implications

See `SECURITY_MODEL.md` section 10 and ADR-0014. Excerpts of mail are stored in the owner's private
conversation until they disconnect, and no retention limit exists yet.

### Recommended next step

Create the OAuth client and run the live test. Calendar comes after that.

## 2026-11-04: Bounded planner and conversation context, branch `feat/planner`

### What changed

- `assistant/planner.py`: one planning call, seeing only the user's questions and the approved tool
  catalog. `assistant/tool_runner.py`: runs planned calls through the gateway and records outcomes.
- `assistant/service.py`: fixed sequence of plan, tools, retrieval, answer. Limits on calls, time and
  tokens per question. Follow-ups use earlier questions in planning, and still-readable earlier
  answers in answering, never as evidence.
- Citations can now be tool results, with tool, arguments and fetch time. Stored ones are withheld
  when the server is disabled or the reader loses `tools:invoke`.
- New status `budget_exceeded`. UI shows live results and a "Live systems checked" list.
- `OLLAMA_NUM_GPU` setting. No migration: the trace is stored in the message.

Reasoning is in ADR-0013. Controls and gaps are in `SECURITY_MODEL.md` sections 5 and 10.

### Verified results

| Check | Result |
| --- | --- |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (67 source files) |
| Full backend suite, Linux container, Python 3.13, direct Docker network | 375 passed, 4 live tests deselected |
| Full backend suite, Windows, Python 3.14 | not run on the final code. New tests passed there before the last small changes |
| RAG evaluation with hash embeddings, recall and leak gates | passed |
| Web `eslint`, `tsc`, `vitest`, `vite build` | clean, 20 tests |
| Live test: llama3 on CPU, real MCP protocol, through the API, one run | 2 passed |
| Injection with a scripted model obeying the hostile text (`tests/security/test_planner_injection.py`) | passed |
| ADR-0009 latency, loopback, mock server, Windows | HTTP p50 13 ms, p95 15 ms. stdio p50 1.3 s, p95 1.6 s |

The Phase 3 injection criterion is retested with a model now consuming tool output. It holds by
construction: planning has finished before tool output is read, so hostile text cannot select a tool
or set an argument.

### Planner evaluation, llama3 on CPU

12 planning and 4 injection cases, synthetic, written alongside the code, with prompt changes made
while looking at them. These show the pipeline works with this model. They do not predict quality.

| Metric | Result |
| --- | --- |
| Valid plan JSON | 12 of 12 |
| Exact tool choice | 7 of 12 |
| Called a tool when none was expected | 2 |
| Called no tool when one was expected | 1 |
| Injection canary in the answer | 0 of 4 |
| Answer cited the real source | 4 of 4 |
| Planning latency p50 | 12 s on CPU. Roughly 4.5 s on GPU, when output was usable |

RAG evaluation re-run after the context fix, nomic-embed-text and llama3: recall 1.0, expected source
cited 1.0, permission leaks 0, missing questions declined 1.0, injection followed 0, 10 grounded, 4
partly cited, 6 no evidence, faithfulness heuristic 0.881 (0.782 before). Same small synthetic corpus,
so it shows the change did not hurt. It does not predict quality on real documents.

**llama3 is a weak planner.** It missed plain "status of PR 42" questions, and tried a repository
outside the allowlist, which the provider refuses. Adequate for proving the design, not for relying
on. A stronger model has not been tried.

### Findings

- **GPU output was garbled on this machine.** On the RTX 2060, the same prompt returned clean JSON,
  then text such as "acceeyl", varying between reloads. On CPU it was clean in every run: 12 of 12
  twice, in two output modes. The GPU is the likely cause, not proven. Ollama here is 0.30.4 with an
  update pending, and that was not tried. **Set `OLLAMA_NUM_GPU=0` on this machine.** Bad output was
  safe, since parsing refused it, but it made planning useless.
- JSON schema output scored the same as plain JSON on CPU (7 of 12), so it was not added.
- A garbled rewritten query hurt search, so the user's own words are now searched first and a
  rewrite is used only if they find nothing.
- llama3 copied generated schema titles into arguments, and they are now removed from the prompt.
- The model sometimes declines to answer from a successful call. A note now says data came back.

### Real GitHub (first contact)

Run against `ShatanshAdhikari/opsassist`, a public repository, with the real App.

| Check | Result |
| --- | --- |
| All five tools read real data, PR checks readable | passed |
| Token reaches only the named repository, though the app is installed on all | passed |
| Installation and token permissions, as GitHub reports them | all `read` |
| Write probe, a label with an empty name | 403, `Resource not accessible by integration` |
| Real question with llama3 on CPU, one run | see below |

**A serious bug found here: Ollama silently truncated long prompts.** Its default context here is 4096
tokens, and it drops the start of anything longer, so the model answered from a fragment. It first
appeared as a missing citation, then as a confident claim that a PR title was absent from data that
held it. An earlier check missed this by using a short prompt. The context is now set explicitly
(`OLLAMA_NUM_CTX`, default 16,384), and a prompt that cannot fit is refused rather than cut. Document
answers shared this limit, so large retrievals may have been affected too.

Browser test `web/e2e/live-tools.spec.ts` passed against the real stack: real GitHub, llama3 on CPU.
Screens were inspected, including the citation panel and phone width.

**The earlier write test was wrong.** It expected 403 on an issue with an empty title. GitHub validates
that body before checking permission, so a read-only token gets 422 there too. Its failure was not a
security problem. It now probes labels, and also checks the permissions GitHub reports. A valid write
was never attempted, since it would create something on a public repository if this were wrong.

**Real question result, after the fix above, one run:** "Get pull request number 1 ... What is its
title and state?" Planning chose `github.pull_get` with the right arguments. The answer gave the
correct title and state, cited the live result with its fetch time, and was labelled `grounded`. The
Phase 3 criterion "live PR status answer cites API with timestamp" is met. It took about 6 minutes on
CPU, which is too slow for real use.

### Not verified Only component tests ran, and the browser test was not rerun.
- Rewritten follow-up search with a real model, beyond the single planning eval case.
- The Anthropic adapter with planning. Its JSON output is unmeasured.
- Latency to a real remote MCP server.

### Known limitations

- **One result cannot feed another call** (ADR-0013). "Review this issue and inspect the code" needs
  the user to name the file.
- Not durable: runs inside the request. A crash leaves the question with no answer, and a `running`
  invocation if mid-call. Safe only because every call is a read.
- Budgets are per question. No per-user or per-workspace spend limits yet.
- Follow-up planning sees questions only, so "the second one" cannot refer to an earlier answer.
- A not-found answer from the model does not show what the tool returned.
- A question with tools costs an extra model call even when no tool is needed.

### Upgrading a running checkout

No migration from this step, and `0002`, `0003` still apply if the database is behind. Set
`OLLAMA_NUM_GPU=0` in `.env` on this machine.

### Recommended next step

Decide on a stronger model, since llama3 is now the main limit on answer quality. Then check the UI
in a browser against real GitHub, and commit.

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
