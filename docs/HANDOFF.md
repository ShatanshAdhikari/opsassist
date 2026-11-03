# Handoff

Written 2026-11-04. Start here when resuming. This is a snapshot, so check it against `git status`,
`git log` and `gh pr view 1` before relying on it. The full requirements are in `CLAUDE.md`, and
verified results are in `docs/PROGRESS.md`.

## Where things stand

OpsAssist is an auditable AI operations platform. A person can upload documents and get cited,
permission-filtered answers in the UI. A read-only tool layer and GitHub tools exist behind the API,
but **the assistant cannot use them yet** and they have no screen.

| Piece | State | Proven against |
| --- | --- | --- |
| Foundation and document Q&A (Phases 0 to 2) | On `main` | Real Postgres, Ollama, browser test |
| Tool layer and MCP client | On `main` (PR #1) | Mock MCP server, real protocol |
| Encrypted credentials, read-only GitHub | On `main` (PR #2) | Fake GitHub only |
| Planner, conversation context, Gmail, Calendar, scheduled tracking, approvals | Not started | |

### Git

- Repo: <https://github.com/ShatanshAdhikari/opsassist>. **Public**, by the owner's choice.
- PR #1 and PR #2 were merged into `main` at the owner's request. Work from `main`, on a new branch.
- `feat/tool-layer` and `feat/github-readonly` were left in place, and can be deleted.
- Merging did not settle ADR-0012 or prove real GitHub behaviour: see the decisions below.

### Step 2 verification at time of writing

- Windows, Python 3.14: 308 passed. Linux container, Python 3.13: 308 passed.
- Migrations clean. ruff, format, mypy and lockfile checks clean.
- Live GitHub test not run: it needs the app from decision 1 below.

## Decisions waiting on the owner

1. **GitHub App.** Must be created in a browser: `docs/GITHUB_SETUP.md`. It can be installed on
   `opsassist`, so no new repo is needed. Ask for App ID, installation ID and the `.pem` **path**,
   never its contents. Then run the live test.
2. **ADR-0012.** Any workspace member can read every repo on a connection, even a private one they
   could not open on GitHub. Recommended: accept for now with one workspace, and add per-person
   checks before repos need different audiences. Not yet agreed.
3. **Google account.** The owner's account looks like personal Gmail, which cannot be an internal
   Workspace app. An OAuth app in testing mode works for personal use, but refresh tokens expire
   after 7 days. Public release needs Google's restricted-scope review. Needed before Gmail.
4. ADRs 0001 to 0012 are still provisional, pending the owner's review.

Open product decisions are also listed in `docs/IMPLEMENTATION_PLAN.md`.

## Next work, in the agreed order

1. Finish step 2: run the live GitHub test, and fix whatever it finds.
2. **Conversation context and bounded planner**, so the assistant can use tools. Limits on tool calls,
   time and tokens. Answers cite tool results with source and fetch time. Tool output stays
   untrusted, and the injection criterion must be retested, since it holds trivially until a model
   consumes output. Measure per-call latency first (ADR-0009).
3. Gmail, read-only (`gmail.readonly`), needing OAuth refresh and revocation.
4. Calendar, read-only.
5. Scheduled tracking: re-discovery, sync, rules such as "unanswered for 3 days", digest.
6. Phase 4 approvals. **Re-evaluate Temporal first** (ADR-0003). Write tools stay refused by
   `not_callable_reason` in `tools/registry.py` until then.

Screens are also missing: integrations, members, audit, grants.

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
- **Dev database is at migration `0001`, with 1 user, 1 workspace, 0 documents.** Before restarting on
  this branch: stop the API and worker, sync, then `alembic upgrade head`. No password is recorded
  anywhere, and there is no reset flow.
- `.env` has Ollama for models, and has **no `CREDENTIAL_KEYS`**, so connections return 503 until set.
- Installed by this project: `uv` (pip --user), `nomic-embed-text`, Playwright Chromium, Docker image
  `python:3.13-slim`. `web/node_modules` sits in OneDrive and should be excluded from sync.
- **`gh` has two accounts.** `ShatanshAdhikari` is active and owns the repo, with `workflow` scope
  added. `shatansh-stack` is the other. Check with `gh api user --jq .login` before creating anything.

## Traps already hit

- `!` commands in Claude Code run in **bash**, so `$env:` fails there. Long-running processes should
  go in their own window.
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

## Unresolved

- **Intermittent 60-second database connect hang**, in the Linux container through
  `host.docker.internal` only: 2 of 5 runs, against 0 of 7 over the direct network. Likely Docker
  Desktop, not proven. Prefer the direct network.
- Python versions differ: image 3.13, CI and local 3.14. Both pass, but nothing pins them together.
- Anthropic adapter has never called the real API. It has no key, and data egress needs
  `EXTERNAL_MODEL_DATA_APPROVED=true`, an owner decision.
- Load, backup and restore are untested. RAG evals used a small synthetic corpus, also used while
  building, so they say little about real documents.
- Security gaps are listed in `docs/SECURITY_MODEL.md` sections 10 and 11.

## How the owner wants to work

- May be AFK and wants work to continue, without asking for bash permission. Ask only what truly
  blocks progress.
- Commit, push and open PRs only when asked. Confirm before anything outward-facing.
- Report what was actually verified, and name what was not. Say so when an earlier claim was wrong.
- Replies are terse. `CLAUDE.md` sets the engineering standard, and each feature's record goes in
  `docs/PROGRESS.md`.
