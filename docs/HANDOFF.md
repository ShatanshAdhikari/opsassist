# Handoff

Written 2026-11-04, updated 2026-11-05 and early 2026-11-17. Start here when resuming. This is a snapshot, so
check it against `git status`, `git log` and `gh pr view 3` before relying on it. The full
requirements are in `CLAUDE.md`, and verified results are in `docs/PROGRESS.md`.

## Resume here

**Update 2026-11-16: GPU test done. Stay on CPU** (`OLLAMA_NUM_GPU=0` kept). CUDA still garbles on
0.40.15, and Vulkan is clean for prose but misspells repository names in 1 of 3 plans. Details in
`docs/PROGRESS.md`. Not tried: a model that fits fully in VRAM.

**Session ended early 2026-11-17** with the owner asking to close VS Code. The real questions were not
asked. The API, worker and web were started from the assistant's shell, so closing VS Code most likely
stopped them: start them again with "Running it" below. Docker Desktop, the `db` container and Ollama
(normal start, no `CUDA_VISIBLE_DEVICES`) were left running. The `PROGRESS.md` and `HANDOFF.md`
changes are **uncommitted** on `feat/planner`. Commit only when the owner asks.

Optional next experiment, offered and not yet agreed: pull `llama3.2:3b` or `qwen2.5:3b` (about 2 GB,
free) and run the GPU test on it, to see whether partial offload causes the garbling.

1. **Make sure Ollama is running:** `curl http://localhost:11434/api/version` should print
   `0.40.15`. If not, start it with `Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"`.
   It does **not** start at sign-in. Suggest the owner enable that.
2. **Ask real questions end to end** and record the results: "What pull requests are open in
   ShatanshAdhikari/opsassist?" (should cite PR #3 with a fetch time), and one question of real mail.
   **PR question done 2026-11-17:** cited PR #3 from a live `github.pulls_list` call with a fetch
   time, in about 3 minutes (`docs/PROGRESS.md`). **Mail question still to do.** Needs the owner's
   sign-in. Logs are in `%LOCALAPPDATA%\opsassist\api.log`, `worker.log` and `web.log`.
3. **PR #3** is open and CI passes. **Merge only when the owner asks.** Then Calendar.

### Tonight's events (2026-11-05)

- `.env` now has the Google client, `CREDENTIAL_KEYS` (generated, never displayed), and
  `CREDENTIAL_ACTIVE_KEY_ID=k1`, plus `OLLAMA_NUM_GPU=0`. The owner should back up the key separately.
  The client secret was pasted into chat. The owner was told they can reset it in Google Console.
- The dev database was upgraded from `0001` to `0004`. The owner signed in with their existing password.
- **Gmail connected** by the owner through the screen, with real Google, both tools approved.
- **GitHub screen built** (`web/src/screens/GitHubConnections.tsx`), and the page intros were
  rewritten. The owner connected the real App: app 5613304, installation 178596510, repo
  `ShatanshAdhikari/opsassist`. Verified healthy, 5 tools approved. "Not checked yet" was shown at
  first only because the API had not been restarted after the backend change. Fixed: an unpaired
  server now shows "Status unknown".
- **History rewrite done** by the owner. Remote `main` is `0663432`, with 0 AI mentions on remote
  branches. `feat/planner` was rebased with identical trees, pushed, and opened as **PR #3**. CI
  passes, backend and web.
- **The first real PR question failed:** "No sources found", because Ollama was not running at all
  (the planner failed within 5 seconds, and so did embeddings). I started it. The owner asked twice
  more on CPU, and those two answers were cancelled when Ollama was restarted for the update. Their
  stored result was not checked.
- **Ollama updated from 0.30.4 to 0.40.15** with the installer it had already downloaded, which has a
  valid Ollama Inc. signature. The GPU garbling was seen on 0.30.4, so this update is what is under test.
- **Free only, from the owner:** no paid APIs, models, hosting or Google verification. The Anthropic
  adapter stays unused.

## Where things stand

OpsAssist is an auditable AI operations platform. A person can upload documents and get cited,
permission-filtered answers in the UI. On `feat/planner` (PR #3), the assistant can also use approved
read-only tools and cite their live results, and a person can connect their own Gmail and GitHub
from the Integrations screen.

| Piece | State | Proven against |
| --- | --- | --- |
| Foundation and document Q&A (Phases 0 to 2) | On `main` | Real Postgres, Ollama, browser test |
| Tool layer and MCP client | On `main` (PR #1) | Mock MCP server, real protocol |
| Encrypted credentials, read-only GitHub | On `main` (PR #2) | Real GitHub, public repo |
| Planner, conversation context | PR #3, CI passes | Real GitHub, llama3 on CPU, browser test |
| Read-only Gmail, Integrations screen | PR #3 | Real Google sign-in. No real answer from mail yet |
| GitHub connection screen | PR #3 | Owner connected the real App in the browser |
| Calendar, scheduled tracking, approvals | Not started | |

### Git

- Repo: <https://github.com/ShatanshAdhikari/opsassist>. **Public**, by the owner's choice.
- PR #1 and PR #2 were merged into `main` at the owner's request. PR #3 (`feat/planner`) is open.
- `feat/tool-layer` and `feat/github-readonly` were left in place, and can be deleted.
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
2. **ADR-0012.** Any workspace member can read every repo on a connection, even a private one they
   could not open on GitHub. Recommended: accept for now with one workspace, and add per-person
   checks before repos need different audiences. Not yet agreed.
3. **Google account.** Decided: personal Gmail, OAuth app in Testing mode, local model. Refresh tokens
   expire after 7 days, accepted, so the owner reconnects weekly. The client is created and Gmail is
   connected. `tests/integration/test_gmail_live.py` is optional.
4. ADRs 0001 to 0014 are still provisional, pending the owner's review. ADR-0013 needs a real look:
   one tool result cannot feed another call, by design, to keep injected content away from tools.
5. **Stronger model.** llama3 chose the exact tools in 7 of 12 cases, and takes about 6 minutes per
   question on CPU. **Decided 2026-11-05: free only, no money spent on this project.** Paid external
   models are out, so the Anthropic adapter stays unused. Ollama is now updated to 0.40.15, and the GPU
   test is next (see "Resume here"). If the GPU still garbles, try free smaller models such as
   `llama3.2:3b` or `qwen2.5:3b`: faster on CPU, but likely weaker at choosing tools.

Open product decisions are also listed in `docs/IMPLEMENTATION_PLAN.md`.

## Next work, in the agreed order

1. Done: live GitHub test run, and the one wrong test fixed.
2. Done, in PR #3: planner and conversation context.
3. Connected, unproven: Gmail read-only and the GitHub screen. Prove each with a real question.
4. Calendar, read-only. Personal, like Gmail (ADR-0014), and can share the Google sign-in code.
5. Scheduled tracking: re-discovery, sync, rules such as "unanswered for 3 days", digest.
6. Phase 4 approvals. **Re-evaluate Temporal first** (ADR-0003). Write tools stay refused by
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
outside that process tree, logging to `%LOCALAPPDATA%\opsassist`. Run it once only.

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
- **The app may be stopped.** The owner asked for VS Code to be closed at the end of this session,
  which ends any API, worker or web servers running in its terminals. The `db` container and Docker
  Desktop keep running. Start things with the commands above.
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
  it on CPU: clean, but about 6 minutes per answer, planning about 12 seconds. Retested on 0.40.15
  (2026-11-16): CUDA still garbles, Vulkan misspells names. If model output looks wrong, suspect
  this before the prompt.
- `xuno-discovery-pg` is another project's container on port 5434. Leave it alone.
- Installed by this project: `uv` (pip --user), `nomic-embed-text`, Playwright Chromium, Docker image
  `python:3.13-slim`. `web/node_modules` sits in OneDrive and should be excluded from sync.
- **`gh` has two accounts.** `ShatanshAdhikari` is active and owns the repo, with `workflow` scope
  added. `shatansh-stack` is the other. Check with `gh api user --jq .login` before creating anything.

## Traps already hit

- `!` commands at the assistant's prompt run in **bash**, so `$env:` fails there. Long-running
  processes should go in their own window.
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
  repeat adds about 6 minutes on CPU.

## Unresolved

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
