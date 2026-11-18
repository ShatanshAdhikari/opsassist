# Progress Log

Records what was verified, not what was intended. Newest first.

## 2026-11-18: ADRs accepted, and two small free models tried on the planner eval

**ADRs.** The owner accepted ADR-0001 to ADR-0014, including ADR-0012 with its limit. The follow-ups
they record still stand. `docs/adr/README.md` is updated.

**Small models.** Pulled `qwen2.5:3b` (1.9 GB) and `llama3.2:3b` (2.0 GB), free. Ran
`evals/run_planner_eval.py` once per model on GPU (default offload) and once on CPU (`--num-gpu 0`),
Ollama 0.40.15, RTX 2060. The same 12 planning and 4 injection cases as llama3, one run each.

| Model, device | Valid JSON | Exact tools | Unneeded calls | Missed calls | Planning p50 | Injection canary | Cited source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| llama3 8B, CPU (2026-11-04) | 12 of 12 | 7 of 12 | 2 | 1 | 12.4 s | 0 of 4 | 4 of 4 |
| qwen2.5:3b, GPU | 12 of 12 | 9 of 12 | 1 | 0 | 1.1 s | 0 of 4 | 4 of 4 |
| qwen2.5:3b, CPU | 12 of 12 | 7 of 12 | 3 | 0 | 4.7 s | 0 of 4 | 4 of 4 |
| llama3.2:3b, GPU | 12 of 12 | 7 of 12 | 4 | 0 | 1.1 s | 0 of 4 | 4 of 4 |
| llama3.2:3b, CPU | 12 of 12 | 7 of 12 | 4 | 0 | 6.5 s | 0 of 4 | 4 of 4 |

What this shows:
- **The GPU works for models that fit in it.** The Ollama log shows full offload (37 of 37 and 29 of
  29 layers, CUDA), and every plan was valid JSON. llama3 8B garbled on this GPU, and it does not fit
  fully in 6 GB. So partial offload is the likely cause of the garbling. That is better supported now,
  but still not proven.
- **Neither small model is clearly a better planner.** Both match llama3's 7 of 12 on CPU. qwen2.5's
  9 of 12 on GPU is one run at temperature 0.1, and the same model scored 7 on CPU, so treat the gap as
  noise until it repeats.
- Shared misses: both models call a tool for questions that only need documents (policy, chitchat),
  both try the repository outside the allowlist, which the provider refuses, and both list PRs instead
  of fetching the two named ones. qwen2.5 also lists PRs for "status of PR 42".
- **Answers are weaker.** One qwen2.5 GPU answer was only `[S1]`, with no text. No invented citations
  and no injection canary in any run.
- About 1 s per plan on GPU against 12 s for llama3 on CPU in this eval. On the real app, planning for
  llama3 is about 80 s on CPU, so the gap there should be larger. Not measured in the app.

### App switched to qwen2.5:3b on GPU, first real mail question

At the owner's request, `.env` now has `LLM_MODEL=qwen2.5:3b` and `OLLAMA_NUM_GPU` commented out. The
owner asked "Summarize the emails I received this week that need a reply." Checked in the database.

- **45.5 s** for the whole question, against 3 min 20 s for llama3 on CPU on 2026-11-17.
- The planner chose `gmail.threads_search` with `is:unread newer_than:1w`. **Gmail accepts only d, m
  and y**, so it matched nothing: 132 characters of output, against 11,864 the day before. The answer
  was "I could not find this in the available sources. [S1]", which is true of the empty result, but
  wrong about the mailbox.
- **Fix:** `gmail_query` in `gmail_provider.py` rewrites `newer_than:Nw` and `older_than:Nw` as days
  before calling Gmail, and the tool description names the valid units. The query Gmail ran is now in
  the result and the model's text, since it can differ from the planned arguments. Tests: 5 unit cases
  for the rewrite, 1 for the rendering, 1 integration test against the fake Gmail. Ruff, format and
  mypy clean. `tests/integration/test_gmail.py` plus all unit tests: 223 passed (Windows). The full
  suite and the Linux run were not repeated.
- Also seen, not fixed: a "could not find" answer still carried a citation `[S1]` and was marked
  `grounded`. Harmless here, but a not-found answer should not cite.

- **Second ask, after the fix: denied, as designed.** Changing the tool description changed its
  definition hash, so the gateway refused the call (`tool_definition_changed`, 3.5 ms) and marked
  `gmail.threads_search` as `changed` and disabled, with the new definition pending review. The
  answer said no source was found and generated nothing, which is honest. An admin must approve the
  new definition on the Integrations screen before mail works again.

- **Third ask, after the owner approved the new definition: search works, answer useless.** Same
  plan (`is:unread newer_than:1w`), now sent to Gmail as `newer_than:7d`: 16,658 characters back in
  1.6 s. The answer was only "Inference: [S1]", in 5.7 s for the whole question. The model cited
  S2 to S5, which do not exist, and the validator removed them. Ollama's log shows the answering
  prompt was 2,348 tokens, not truncated, and the model wrote about 20 tokens. So qwen2.5:3b plans
  quickly but cannot write this answer. It matches the eval, where one qwen answer was only `[S1]`.

- **Fourth ask, `llama3.2:3b` on GPU: 18.8 s, empty again.** It planned
  `in:inbox newer_than:7d awaits:reply`. `awaits:reply` is not a Gmail operator. It echoes the tool
  description's "awaits a reply", and Gmail searched it as text: 180 characters back, and the answer
  was "I could not find this in the available sources."
- **Second fix:** `gmail_query` now also drops operators Gmail does not document, and the result
  says which terms were left out, under the query that ran. Reply state is computed by OpsAssist, so
  dropping such a term loses nothing. The tool description is unchanged, so no re-approval was
  needed. Tests: 12 unit cases for the query, 2 for the rendering, 2 integration tests. Ruff,
  format, mypy clean. `tests/integration/test_gmail.py`: 43 passed (Windows). API restarted.

- **Fifth ask, `llama3.2:3b` on GPU, with both fixes: first real answer from a small model, 14.8 s.**
  Same plan with `awaits:reply`, dropped before the search. 10 threads came back (32,923 characters,
  2.95 s). The answer listed 4: "You earned 4 points" as awaiting a reply, and three, including
  "Graduate Student Life, Financial Support and More" and "DSAN prerequisite requirements", as
  unclear because they came through a mailing list. Status `partially_grounded`, 5 sentences flagged
  uncited, no invented citations. Problems: it opened with "You have 4 emails that need a reply"
  although only 1 was marked yes, it numbered the last one "Conversation 10", and it mostly copied
  the rendered lines. The other 6 threads were left out, as expected if they were automated or
  answered. Not checked: who sent "You earned 4 points", and so whether the automated-sender check
  should have caught it. The tool output is not stored, by design, so that needs the owner's mailbox.

Not done: no repeated eval runs to measure variance. Raw eval results:
`backend/evals/results/planner-*.json` (untracked; it holds the CPU run of each model).

## 2026-11-17: first real PR question answered end to end

The owner asked "What pull requests are open in ShatanshAdhikari/opsassist?" in the browser, signed
in as themselves, llama3 on CPU. Checked in the database, not only on screen.

- **Answer correct and grounded:** cites PR #3, "Add planner, read-only Gmail and GitHub connection
  screen", as `[S1]`. Grounding status `grounded`, planner `ran`, 0 uncited sentences, 0 removed
  citations.
- **Live, not indexed:** one call to `github.pulls_list` with `{"repo": "ShatanshAdhikari/opsassist",
  "state": "open"}`, succeeded in 842 ms, untruncated. The citation carries `fetched_at`
  `2026-11-17T15:33:02Z` and the invocation ID.
- **Time:** asked 15:31:28, answered 15:34:18 UTC, about 2 min 50 s, faster than the 6 minutes
  expected.
- **Wording is weak:** "According to the excerpt, there is at least one open pull request". It is
  hedged, and the answer does not show the fetch time to the reader. Not fixed.
- **An earlier attempt failed with a 500, caused by the launch, not the app.** The API, worker and web
  were started from the assistant's shell, and Windows ended them with it about a minute later, mid
  answer. Vite logged `read ECONNRESET` and returned 500. Fixed by `scripts/start-opsassist.ps1`,
  which starts them outside the caller's process tree. They survived later shell exits.
- Still not done: a real mail question.

## 2026-11-17: real mail question, four attempts, no useful answer yet

Question: "Summarize my unread emails from the last 3 days". Every attempt planned the right call,
`gmail.threads_search` with `in:inbox is:unread newer_than:3d` (the last three added `in:inbox`).

| Asked (UTC) | Outcome | Cause |
| --- | --- | --- |
| 15:35 | "No reliable source was found" | `reauthorization_required`: the 7-day Testing-mode token from 2026-11-05 had expired. Correctly refused. The owner reconnected Gmail |
| 15:38 | `model_unavailable`, raw excerpt shown | Gmail succeeded (14,313 chars), then `Ollama generation failed: ReadTimeout` at `llm_timeout_seconds` 180 |
| 15:43 | `budget_exceeded`, raw excerpt shown | Planning took 84 s, leaving the answer about 216 s of `assistant_max_seconds` 300 |
| 16:42 | `no_evidence`: "I could not find this in the available sources." | Gmail succeeded (11,864 chars). Ollama logged 5,184 prompt tokens in an 8,192 context, `truncated = 0`, so nothing was cut. llama3 read the threads and still answered not-found |

- **Config change, local only:** `.env` now has `LLM_TIMEOUT_SECONDS=600` and
  `ASSISTANT_MAX_SECONDS=900`, needed for CPU. Not in `.env.example`.
- **The failures were honest.** None invented mail content, and the fallbacks cited the real excerpt.
- **Likely cause of the last one, not proven:** the Gmail result is raw JSON, about 5,000 tokens, which
  is hard for llama3 8B to read, and slow: about 5 minutes of CPU prefill alone. A compact rendering
  (sender, subject, date, snippet per thread) should help with both. Not built.
- llama3's context is 8,192 tokens, below `ollama_num_ctx` 16,384, so Ollama caps it. Larger mail
  results could be truncated.

### Compact Gmail search result for the model: first real mail answer

**What changed:** `integrations/gmail_format.py` renders a `threads_search` result as plain text:
per conversation the subject, message count, unread and last-sender flags, and per message the date,
sender, marks and a 500-character snippet, with the newest 5 messages kept. IDs, labels, links and
to/cc are dropped. `ToolOutput.model_text` carries it, and `tool_runner` prefers it over the JSON.
API callers still get the full JSON. Rendered after redaction. Whitespace in every field is
collapsed so mail cannot forge a line of the layout. `thread_get`, GitHub and MCP are unchanged.

**Tests:** ruff, format and mypy clean. Unit tests 146 passed, plus 8 new in `test_gmail_format.py`.
Integration `test_gmail`, `test_tools`, `test_assistant` and `test_planner`: 127 passed in 10 min,
including 2 new tests (the prompt gets the compact text and no raw JSON, and a secret in mail stays out
of it). The full suite and the Linux run were not repeated.

**Real run** (asked 17:21:12, answered 17:24:32 UTC, 3 min 20 s): "Summarize the emails I received
this week that need a reply." Planner chose `in:inbox newer_than:7d is:unread`. Prompt fell from 5,184
to 1,735 tokens, `truncated = 0`. **First real answer from mail:** it named all 5 conversations
correctly (a Codecademy bootcamp and 4 LinkedIn notifications) and cited `[S1]` with its fetch time.
Status `partially_grounded`: the opening sentence had no citation.

- **Wrong on "need a reply".** All 5 are automated notifications, yet the answer says they need a
  reply. The model took "last message not from the mailbox owner" to mean that, and the planner
  searched for unread mail instead. Not fixed. A code-level check for automated senders (`no-reply`
  addresses, `List-Unsubscribe` headers) would let the result say "automated, no reply expected".

## 2026-11-16: GPU retest on Ollama 0.40.15. Result: stay on CPU

No code changed. Tests ran with `%LOCALAPPDATA%\opsassist\gpu_test.py` and two probes in the
session scratchpad, against local Ollama only, llama3 Q4_0, temperature 0, seed 1, model reloaded
between rounds. The RTX 2060 (6 GB) holds 24 to 26 of 33 layers, so every GPU run is a partial offload.

| Backend | Plan JSON correct | Count to 40 | Prose | Generate speed |
| --- | --- | --- | --- | --- |
| CPU (`num_gpu=0`) | 8 of 8, identical each time | clean | clean | about 6 tok/s |
| CUDA (default) | not reached | "Brunsting!" | looped "(b) (c) (d)..." past 5,600 tokens | about 12 tok/s |
| Vulkan (`CUDA_VISIBLE_DEVICES=-1`) | 6 of 9 | clean 6 of 6 | clean 6 of 6 | about 19 tok/s |

- **CUDA still garbles on 0.40.15**, so the update did not fix it. One CUDA load also crashed outright:
  `CUDA error: shared object initialization failed`, exit `0xc0000409`. The next load of the same
  backend worked.
- **Vulkan failures are subtle**, which makes them worse than plain garbage: a misspelled repository
  (`ShanshAdhikari`, `ShatanshAdharni`), a missing `calls` wrapper, and arguments as a list. At
  temperature 0 the output changed between reloads, while CPU output never did.
- **Decision: keep `OLLAMA_NUM_GPU=0`.** Ollama was restored to its normal start (no
  `CUDA_VISIBLE_DEVICES`). A misspelled repository is refused by the allowlist, but a misspelled
  value in a free-text argument would not be.
- Not tried: a model small enough to fit fully in VRAM (`llama3.2:3b`, `qwen2.5:3b`), which would
  show whether partial offload is the trigger. Not tried: a newer NVIDIA driver.
- Still not done: a real PR question and a real mail question end to end. The app was started for
  the owner to ask them, since asking needs their sign-in.

## 2026-11-05: GitHub connection screen and clearer page intros, branch `feat/planner`

### What changed

- `web/src/screens/GitHubConnections.tsx`, on the Integrations screen, for owners and admins: connect
  a GitHub App (the key is chosen as a file and never displayed), check, find tools, change
  repositories, replace the key, remove, and approve tools one by one or all at once. Members see a
  note that an administrator connects GitHub.
- Approval from the screen is offered only for tools whose provider marks them read-only and not
  destructive. For a changed tool, the pending definition is judged. The backend still refuses to run
  anything that is not read-only at risk 0, whatever was approved.
- `ServerOut` now includes `connection_id`, so the screen pairs each connection with its tools.
- Assistant, Knowledge and Integrations pages now say what each is for. The Assistant's empty state
  no longer claims each question is independent, which stopped being true with follow-up context.
- The dev database was upgraded from `0001` to `0004`, and `.env` now has the Google client,
  `CREDENTIAL_KEYS` and `OLLAMA_NUM_GPU=0`. The owner connected Gmail through the screen.

### Verified results

| Check | Result |
| --- | --- |
| Web `tsc --noEmit`, `eslint` | clean |
| Web component tests, including 13 new for GitHub | 42 passed |
| `ruff check`, `ruff format --check`, `mypy --strict` | clean (71 source files) |
| `test_github.py` and `test_tools.py`, Windows, Python 3.14 | 113 passed |
| Gmail connected by the owner through the screen, real Google | connection row present, server healthy |

### Not verified

- The GitHub screen against a real GitHub App in a browser. The owner has not connected it yet.
- A real question answered from real mail.
- Full backend suite and the Linux run, on this change.

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
  five tools. Access decision in ADR-0012, accepted by the owner 2026-11-18.
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
