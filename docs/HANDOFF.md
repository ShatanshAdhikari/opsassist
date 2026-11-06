# Handoff

Written 2026-11-04. Start here when resuming. This is a snapshot, so check it against `git status`,
`git log` and `gh pr view 1` before relying on it. The full requirements are in `CLAUDE.md`, and
verified results are in `docs/PROGRESS.md`.

## Resume here

Update 2026-11-05: steps 1 and 2 below are done, and Gmail is connected through the screen. A
GitHub connection screen and clearer page intros are built and uncommitted (see `docs/PROGRESS.md`).
Next: the owner restarts the API, connects GitHub on Integrations, and asks real questions.

Waiting on the owner. On branch `feat/planner` at `4f38fd6`, committed locally and **not pushed**.
This handoff update is uncommitted.

1. **Gmail client:** the owner creates the OAuth client (`docs/GMAIL_SETUP.md`, section 1) and adds
   `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` to `.env`.
2. **Which database:** upgrade the dev database, recommended, or use a throwaway one. The dev database
   is at `0001`, has no known password, and upgrading means stopping the owner's app. Ask first. The
   owner sets any new password. Also add `CREDENTIAL_KEYS`, `CREDENTIAL_ACTIVE_KEY_ID` and
   `OLLAMA_NUM_GPU=0` to `.env`.
3. **Then:** the owner clicks through Google consent. Check the Integrations screen in a browser, and
   ask a real question of real mail. Record the results in `docs/PROGRESS.md`.
4. **History push, before pushing `feat/planner`:** otherwise the old commits and their co-author
   trailers go back to GitHub. See the Git section.
5. **Open a PR only when asked.** Then Calendar.

## Where things stand

OpsAssist is an auditable AI operations platform. A person can upload documents and get cited,
permission-filtered answers in the UI. On `feat/planner`, the assistant can also use approved
read-only tools and cite their live results, and a person can connect their own Gmail. Screens for
GitHub connections and for tools outside Gmail are still missing.

| Piece | State | Proven against |
| --- | --- | --- |
| Foundation and document Q&A (Phases 0 to 2) | On `main` | Real Postgres, Ollama, browser test |
| Tool layer and MCP client | On `main` (PR #1) | Mock MCP server, real protocol |
| Encrypted credentials, read-only GitHub | On `main` (PR #2) | Real GitHub, public repo |
| Planner, conversation context | `feat/planner`, not pushed | Real GitHub, llama3 on CPU, browser test |
| Read-only Gmail, Integrations screen | `feat/planner`, not pushed | Fake Google only |
| Calendar, scheduled tracking, approvals | Not started | |

### Git

- Repo: <https://github.com/ShatanshAdhikari/opsassist>. **Public**, by the owner's choice.
- PR #1 and PR #2 were merged into `main` at the owner's request. Work from `main`, on a new branch.
- `feat/tool-layer` and `feat/github-readonly` were left in place, and can be deleted.
- Merging did not settle ADR-0012 or prove real GitHub behaviour: see the decisions below.
- **Attribution rule from the owner: no AI assistant named in commits, branches or PRs,** including
  co-author trailers and footers, whatever a tool's default suggests.
- **History cleanup is pending.** The owner asked for it. PR bodies are clean. Commit messages were
  rewritten in a separate clone, with trees verified identical, but the force-push was blocked by a
  permission check and left for the owner. If `git ls-remote origin main` still shows `151e1b5`, it
  has not run. The owner runs this at the prompt. It force-pushes `main` and both feature branches,
  then rebases `feat/planner` onto the result:

  ```text
  ! bash "$LOCALAPPDATA/opsassist/history-rewrite/push_rewrite.sh"
  ```

  Backup bundle: `%LOCALAPPDATA%\opsassist\history-backup-2026-11-04.bundle`. Old commits stay
  reachable through `refs/pull/*` on GitHub, and removing those needs GitHub Support. `CLAUDE.md` and
  a few docs still contain the word. The owner has not asked for those to change.

### Verification

Current results and what was not verified are in `docs/PROGRESS.md`. The live GitHub test has run
and passes. Set `GITHUB_TEST_APP_ID`, `_INSTALLATION_ID`, `_PRIVATE_KEY_FILE` and `_REPO` to rerun it.

## Decisions waiting on the owner

1. **GitHub App.** Created: app ID 5613304, installation 178596510, key at `key.pem` in the repo root.
   It is installed on all the owner's repositories, though tokens are limited to the connection's
   list. The owner has said the key's handling is fine, so do not raise it again.
2. **ADR-0012.** Any workspace member can read every repo on a connection, even a private one they
   could not open on GitHub. Recommended: accept for now with one workspace, and add per-person
   checks before repos need different audiences. Not yet agreed.
3. **Google account.** Decided: personal Gmail, OAuth app in Testing mode, local model. Refresh tokens
   expire after 7 days, accepted. Still needed: the owner creates the OAuth client from
   `docs/GMAIL_SETUP.md`. `tests/integration/test_gmail_live.py` is optional, since it needs a refresh
   token saved to a file. Going through the screen tests the real flow.
4. ADRs 0001 to 0014 are still provisional, pending the owner's review. ADR-0013 needs a real look:
   one tool result cannot feed another call, by design, to keep injected content away from tools.
5. **Stronger model.** llama3 chose the exact tools in 7 of 12 cases, and takes about 6 minutes per
   question on CPU. **Decided 2026-11-05: free only, no money spent on this project.** Paid external
   models are out, so the Anthropic adapter stays unused. Free options: update Ollama in case that
   fixes the GPU garbling, or try other free local models with better tool calling.

Open product decisions are also listed in `docs/IMPLEMENTATION_PLAN.md`.

## Next work, in the agreed order

1. Done: live GitHub test run, and the one wrong test fixed.
2. Done, not pushed: planner and conversation context.
3. Built, unproven: Gmail read-only. Prove it against real Gmail, and check the Integrations screen.
4. Calendar, read-only. Personal, like Gmail (ADR-0014), and can share the Google sign-in code.
5. Scheduled tracking: re-discovery, sync, rules such as "unanswered for 3 days", digest.
6. Phase 4 approvals. **Re-evaluate Temporal first** (ADR-0003). Write tools stay refused by
   `not_callable_reason` in `tools/registry.py` until then.

Screens are also missing: GitHub connections, general tool review, members, audit, grants.

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
- **Dev database is at migration `0001`, with 1 user, 1 workspace, 0 documents.** This branch needs
  `0004`. Before restarting on it: stop the API and worker, sync, then `alembic upgrade head`. No password is recorded
  anywhere, and there is no reset flow.
- `.env` has Ollama for models, and has **no `CREDENTIAL_KEYS`**, so connections return 503 until set.
- **The owner has accepted the GitHub key sitting in the repo root as `key.pem`.** It is gitignored. Do
  not raise it again.
- **llama3 output garbles on this GPU (RTX 2060).** Set `OLLAMA_NUM_GPU=0` in `.env`. Slower but clean.
  See `docs/PROGRESS.md`. If model output looks wrong, suspect this before the prompt.
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

## Unresolved

- **Ollama truncates silently.** `OLLAMA_NUM_CTX` must hold the evidence limits, or prompts are refused.
  If a model claims evidence lacks something it plainly holds, check this first.

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
