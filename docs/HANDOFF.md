# Handoff

Written 2026-11-04, updated 2026-11-05 and 2026-11-17 (evening). Start here when resuming. This is a
snapshot, so check it against `git status`, `git log` and `gh pr list` before relying on it. The full
requirements are in `CLAUDE.md`, and verified results are in `docs/PROGRESS.md`.

## Resume here

**Session ended 2026-11-17 evening**, with the owner asking to close VS Code. PR #3 and PR #4 are
merged, and local `main` is `07ecb25`, clean apart from this handoff update, which is **uncommitted on
`main`**. Commit it on a branch only when the owner asks.

**The app was left running, and should survive VS Code closing.** The API, worker and web were started
with `Win32_Process.Create` (as `scripts/start-opsassist.ps1` does), so they are outside VS Code's
process tree. Not verified after the close: check `curl http://localhost:8000/readyz` and
`http://localhost:5173` first. Docker Desktop, the `db` container and Ollama (CPU) were left running.
The worker process still predates the PR #4 merge. That does not matter for mail, because the
assistant runs in the API, but restart it before relying on new worker code.

1. **Ask the mail question again** to try the automated-sender check on real mail: "Summarize the
   emails I received this week that need a reply." The last run (before the check) named all 5
   threads correctly but said the Codecademy and LinkedIn notifications need a reply. They should now
   show as automated, not awaiting a reply. Record the result in `docs/PROGRESS.md`.
2. **Gmail sign-in lasts 7 days** (Testing mode). The owner reconnected it on 2026-11-17, so expect
   `reauthorization_required` from about 2026-11-24. Reconnect on the Integrations screen.
3. Then the next work below: the `thread_get` compact rendering, then Calendar.

**2026-11-18:** the owner accepted ADR-0001 to ADR-0014, ADR-0012 included. `qwen2.5:3b` and
`llama3.2:3b` were pulled and run on the planner eval: both work on the GPU, with no garbling, at
about 1 s per plan, but neither is clearly a better planner than llama3. See `docs/PROGRESS.md`.

**The app now runs `llama3.2:3b` on the GPU** (`.env`: `LLM_MODEL=llama3.2:3b`, `OLLAMA_NUM_GPU`
commented out). qwen2.5:3b planned well but could not write the mail answer. Both small models wrote
Gmail queries that match nothing (`newer_than:1w`, `awaits:reply`), so `gmail_query` now rewrites
weeks as days and drops unknown operators. The mail question then took 14.8 s and gave a usable but
sloppy answer: it miscounted, and "You earned 4 points" was marked awaiting a reply. Open: who sent
that, and whether the automated-sender check should have caught it. Offered and not agreed: plan with
a small model, answer with llama3 on CPU. To go back to llama3, set `LLM_MODEL=llama3` and
`OLLAMA_NUM_GPU=0`.

### This session's events (2026-11-17)

- **Restart and a 500.** Ollama and the `db` container had stopped. The API, worker and web were then
  started with `Start-Process` from the assistant's shell, which Windows ended with it about a minute
  later, mid answer, so the owner saw a 500. Fixed by `scripts/start-opsassist.ps1`.
- **First real PR answer:** cited PR #3 from a live `github.pulls_list` call, with a fetch time.
- **Mail took four attempts and two code changes.** Expired Gmail token (owner reconnected), then two
  time limits, then llama3 reading 5,000 tokens of JSON and answering "not found". The compact
  rendering cut the prompt to 1,735 tokens and gave the first real mail answer in 3 min 20 s. Details
  in `docs/PROGRESS.md`.
- **Local `.env` additions for CPU:** `LLM_TIMEOUT_SECONDS=600` and `ASSISTANT_MAX_SECONDS=900`. Not in
  `.env.example`. Without them, mail answers time out.
- **PR #3 merged** (`6975065`). **PR #4 merged** (`07ecb25`): compact Gmail search result for the
  model, automated-sender and mailing-list detection, the start script, and the docs. It was reviewed
  with 5 inline comments, all fixed in `4a53704` and resolved. CI passed on the final commit.

## Where things stand

OpsAssist is an auditable AI operations platform. A person can upload documents and get cited,
permission-filtered answers in the UI. The assistant can also use approved read-only GitHub and Gmail
tools and cite their live results, and a person can connect their own Gmail and GitHub from the
Integrations screen. All of this is on `main`.

| Piece | State | Proven against |
| --- | --- | --- |
| Foundation and document Q&A (Phases 0 to 2) | On `main` | Real Postgres, Ollama, browser test |
| Tool layer and MCP client | On `main` (PR #1) | Mock MCP server, real protocol |
| Encrypted credentials, read-only GitHub | On `main` (PR #2) | Real GitHub, public repo |
| Planner, conversation context | On `main` (PR #3) | Real GitHub, llama3 on CPU, browser test |
| Read-only Gmail, Integrations screen | On `main` (PR #3) | Real mail answered 2026-11-17 |
| GitHub connection screen | On `main` (PR #3) | Real PR question answered 2026-11-17 |
| Compact Gmail search, sender kinds | On `main` (PR #4) | Compact result on real mail. Sender kinds not yet tried on real mail |
| Calendar, scheduled tracking, approvals | Not started | |

### Git

- Repo: <https://github.com/ShatanshAdhikari/opsassist>. **Public**, by the owner's choice.
- PRs #1 to #4 were merged into `main` at the owner's request, all as merge commits. No PR is open.
- `feat/tool-layer`, `feat/github-readonly`, `feat/planner` and `feat/gmail-compact-results` were
  left in place, and can be deleted.
- The owner asked for PR #4 to be reviewed with inline comments, fixed, resolved and merged in one go.
  That approval was for PR #4 only: ask again before merging the next one.
- Merging did not settle ADR-0012 or prove real GitHub behaviour: see the decisions below.
- **Attribution rule from the owner: no AI assistant named in commits, branches or PRs,** including
  co-author trailers and footers, whatever a tool's default suggests.
- **History cleanup is done** (2026-11-05, run by the owner). Remote `main` is `0663432`, and no
  remote branch mentions an assistant. Do not run `push_rewrite.sh` again: its leases would refuse,
  and nothing is left to rewrite.
  Backup bundle: `%LOCALAPPDATA%\opsassist\history-backup-2026-11-04.bundle`. Old commits stay
  reachable through `refs/pull/*` on GitHub, and removing those needs GitHub Support. `CLAUDE.md` and
  a few docs still contain the word. The owner has not asked for those to change.

### Verification

Current results and what was not verified are in `docs/PROGRESS.md`. The live GitHub test has run
and passes. Set `GITHUB_TEST_APP_ID`, `_INSTALLATION_ID`, `_PRIVATE_KEY_FILE` and `_REPO` to rerun it.

## Decisions waiting on the owner

1. **GitHub App.** Created and connected in the dev app: app ID 5613304, installation 178596510, key at
   `key.pem` in the repo root. It is installed on all the owner's repositories, though tokens are
   limited to the connection's list (`ShatanshAdhikari/opsassist`). The owner has said the key's
   handling is fine, so do not raise it again.
2. **ADR-0012.** Accepted 2026-11-18, with its limit: add per-person checks before repos need
   different audiences.
3. **Google account.** Decided: personal Gmail, OAuth app in Testing mode, local model. Refresh tokens
   expire after 7 days, accepted, so the owner reconnects weekly. The client is created and Gmail is
   connected. `tests/integration/test_gmail_live.py` is optional.
4. ADRs 0001 to 0014: accepted 2026-11-18. ADR-0013's limit stands: one tool result cannot feed
   another call, to keep injected content away from tools.
5. **Stronger model.** llama3 chose the exact tools in 7 of 12 cases, and takes about 6 minutes per
   question on CPU. **Decided 2026-11-05: free only, no money spent on this project.** Paid external
   models are out, so the Anthropic adapter stays unused. The GPU test on 0.40.15 failed, so llama3
   stays on CPU. Answers now take about 3 minutes, planning about 80 seconds. Free smaller models such
   as `llama3.2:3b` or `qwen2.5:3b` are faster, but likely weaker at choosing tools.

Open product decisions are also listed in `docs/IMPLEMENTATION_PLAN.md`.

## Next work, in the agreed order

1. Done: live GitHub test run, and the one wrong test fixed.
2. Done, in PR #3: planner and conversation context.
3. Done 2026-11-17: Gmail and GitHub each proven with a real question. PR #4 made mail answerable on
   CPU. Still to try: the sender kinds on real mail (see "Resume here").
4. Compact rendering for `gmail.thread_get` (follow-up below).
5. Calendar, read-only. Personal, like Gmail (ADR-0014), and can share the Google sign-in code.
6. Scheduled tracking: re-discovery, sync, rules such as "unanswered for 3 days", digest.
7. Phase 4 approvals. **Re-evaluate Temporal first** (ADR-0003). Write tools stay refused by
   `not_callable_reason` in `tools/registry.py` until then.

Screens are also missing: general tool review (MCP servers), members, audit, grants.

Follow-up from the PR #4 review: `gmail.thread_get` still gives the model raw JSON, up to 30
messages of 6,000 characters each. It needs a compact rendering like search's
(`integrations/gmail_format.py`) before it is usable on CPU.

## Running it

From PowerShell. `uv` is not on PATH, so use `python -m uv`. Set the environment variable in each new
window, or `uv` builds a virtualenv inside OneDrive.

```powershell
docker compose up -d --wait db     # from the repo root: Postgres on localhost:5433
cd backend
$env:UV_PROJECT_ENVIRONMENT="$env:LOCALAPPDATA\opsassist\venv"
python -m uv sync --extra anthropic
python -m uv run alembic upgrade head
python -m uv run uvicorn opsassist.asgi:app --port 8000 --no-access-log
python -m uv run opsassist worker   # second window
```

```powershell
cd web; npm run dev                 # third window: http://localhost:5173
```

**From the assistant's shell, use `scripts\start-opsassist.ps1` instead** (after `db` and Ollama are
up). Processes started with `Start-Process` from the assistant's shell are ended with it: on
2026-11-17 the API died mid answer and the owner saw a 500. The script starts the API, worker and web
outside that process tree, logging to `%LOCALAPPDATA%\opsassist`. It refuses to run while the API, web
or a worker is already up.

**Restarting only the API while the worker runs:** use `python -m uv run --no-sync uvicorn ...`.
Without `--no-sync`, `uv` tries to update the venv after a code change, fails because the running
worker locks `opsassist.exe` (`os error 32`), and the API never starts. The package is an editable
install, so source changes still load. Restarting the API overwrites `api.log`, so copy it first.

### Testing

```powershell
cd backend
$env:UV_PROJECT_ENVIRONMENT="$env:LOCALAPPDATA\opsassist\venv-dev"
python -m uv run ruff check . ; python -m uv run ruff format --check . ; python -m uv run mypy
python -m uv run pytest -q      # 308 tests took 15 minutes on Windows; run it in the background
```

Linux is far faster and matches production: mount `backend` read-only into `python:3.13-slim` on the
`opsassist_default` network, with `TEST_DATABASE_URL` and `TEST_MIGRATION_DATABASE_URL` pointing at
`db:5432`, as the application and owner roles. Tests use only the `opsassist_test` database.

## This machine

- **Two virtualenvs:** `%LOCALAPPDATA%\opsassist\venv` is the owner's running app, and `venv-dev` is
  for development. Windows locks a venv's executables while they run, so **do not sync `venv` or stop
  the owner's processes** without asking.
- **The app may be stopped.** Servers in VS Code terminals end when VS Code closes. Servers started by
  `scripts/start-opsassist.ps1` or `Win32_Process.Create` should not, but a reboot or sign-out ends
  them all, and neither Docker Desktop nor Ollama starts at sign-in. Check before asking questions.
- **Dev database is at `0004`**, with 1 user, 1 workspace, 0 documents, and Gmail and GitHub
  connections. The owner knows their password. None is recorded, and there is no reset flow.
- `.env` is complete for this branch: Ollama, the Google client, `CREDENTIAL_KEYS`/`k1`, and
  `OLLAMA_NUM_GPU=0`. Losing `CREDENTIAL_KEYS` makes the stored GitHub key and Gmail token unreadable.
- **Ollama is the Windows app** (`%LOCALAPPDATA%\Programs\Ollama`), not Docker, and it does not start
  at sign-in. Check it first whenever answers fail fast with "no plan could be made" or "semantic
  search was unavailable". Version 0.40.15, models `llama3` and `nomic-embed-text`.
- **After any backend change, restart the API.** uvicorn runs without `--reload`. The web dev server
  reloads by itself.
- **The owner has accepted the GitHub key sitting in the repo root as `key.pem`.** It is gitignored. Do
  not raise it again.
- **llama3 output garbled on this GPU (RTX 2060, 6 GB) under Ollama 0.30.4.** `OLLAMA_NUM_GPU=0` keeps
  it on CPU: clean. Measured 2026-11-17: about 3 minutes per answer, planning 80 seconds. Retested on 0.40.15
  (2026-11-16): CUDA still garbles, Vulkan misspells names. If model output looks wrong, suspect
  this before the prompt.
- `xuno-discovery-pg` is another project's container on port 5434. Leave it alone.
- Installed by this project: `uv` (pip --user), `nomic-embed-text`, Playwright Chromium, Docker image
  `python:3.13-slim`. `web/node_modules` sits in OneDrive and should be excluded from sync.
- **`gh` has two accounts.** `ShatanshAdhikari` is active and owns the repo, with `workflow` scope
  added. `shatansh-stack` is the other. Check with `gh api user --jq .login` before creating anything.

## Traps already hit

- **Changing a tool's description or schema disables it** until an admin re-approves it on the
  Integrations screen (`tool_definition_changed`). By design. Warn the owner before such a change.
- Stopping the API: match `uvicorn` and `opsassist.asgi` separately. The command line has
  `uvicorn.exe" opsassist.asgi`, so one regex for both misses the real process, which keeps the port.
- `!` commands at the assistant's prompt run in **bash**, so `$env:` fails there. Long-running
  processes should go in their own window.
- `Start-Process` from the assistant's shell is not detached: the child is ended with the tool call's
  process tree. Use `scripts/start-opsassist.ps1` or `Invoke-CimMethod Win32_Process Create`.
- Time limits layer up. `planner_timeout_seconds`, `llm_timeout_seconds` and `assistant_max_seconds`
  all apply, and the whole-question one includes planning. On CPU the defaults (120, 180, 300) are
  too short for mail, so `.env` raises the last two.
- Raw tool JSON is costly for a small model. Five Gmail threads were 5,184 prompt tokens and llama3
  answered "not found". Give the model a compact `ToolOutput.model_text`, as Gmail search now does.
- llama3's context is 8,192 tokens, and Ollama caps `OLLAMA_NUM_CTX` (16,384) to it. Check
  `task.n_tokens` and `truncated` in `%LOCALAPPDATA%\Ollama\server.log` when evidence seems ignored.
- `git commit -F -` with a PowerShell here-string does not pipe it. Use `-m @'...'@`.
- Long bash heredocs with quotes broke once. Write a script file to the scratchpad instead.
- `Set-Location` in a PowerShell tool call persists to the next call.
- Windows hides Linux bugs. CI found a 500 on database outage because asyncio raises plain `OSError`
  there. Run the Linux suite before calling something done.
- jsonschema's default registry can fetch remote `$ref`s, an SSRF risk. Always pass `Registry()`.
- MCP SDK is **2.x**. FastMCP is now `MCPServer`, and 1.x examples do not apply. Read the installed
  source rather than trusting memory.
- Raising inside the SDK's `async with` comes back as an `ExceptionGroup`. Return values out, then
  raise, as `mcp_provider.py` does.
- Test parameters are evaluated at collection, so never compute times there.
- `Start-Process -Wait` on an installer also waits for programs it launches, such as Ollama, so it
  never returns. Check the result instead of waiting on it.
- jsdom has no `File.text()`. Read files with `FileReader`, as `GitHubConnections.tsx` does.
- Asking the same question again while one runs only queues it. Ollama answers one at a time, so each
  repeat adds about 3 minutes on CPU. The owner sent duplicates twice this session.

## Unresolved

- **The Google client secret was pasted into chat on 2026-11-05.** The owner was told they can
  reset it in Google Console. Not known whether they did.
- **Ollama truncates silently.** `OLLAMA_NUM_CTX` must hold the evidence limits, or prompts are refused.
  If a model claims evidence lacks something it plainly holds, check this first.

- **Intermittent 60-second database connect hang**, in the Linux container through
  `host.docker.internal` only: 2 of 5 runs, against 0 of 7 over the direct network. Likely Docker
  Desktop, not proven. Prefer the direct network.
- Python versions differ: image 3.13, CI and local 3.14. Both pass, but nothing pins them together.
- Anthropic adapter has never called the real API, and it will not: the owner has decided on free
  only.
- The full backend suite and the Linux run were not repeated locally on the GitHub screen commit.
  CI on PR #3 passed.
- Load, backup and restore are untested. RAG evals used a small synthetic corpus, also used while
  building, so they say little about real documents.
- Security gaps are listed in `docs/SECURITY_MODEL.md` sections 10 and 11.

## How the owner wants to work

- May be AFK and wants work to continue, without asking for bash permission. Ask only what truly
  blocks progress.
- Commit, push and open PRs only when asked. Confirm before anything outward-facing.
- **Free only.** Never propose paid services. Offer a free alternative when something hits a limit.
- Explain things plainly for someone new to the project. The owner asked for clearer screens for
  newcomers.
- Report what was actually verified, and name what was not. Say so when an earlier claim was wrong.
- Replies are terse. `CLAUDE.md` sets the engineering standard, and each feature's record goes in
  `docs/PROGRESS.md`.
