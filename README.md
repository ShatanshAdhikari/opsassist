# OpsAssist

Auditable AI operations platform. This repository currently implements **Phases 0 to 2**: the
foundation plus one vertical slice: *ask a question over approved documents and get a cited,
permission-filtered answer*. MCP tools, approvals and external integrations are designed but not
built. See `docs/IMPLEMENTATION_PLAN.md` and `docs/PROGRESS.md`.

## Prerequisites

Docker, Python 3.13+, [uv](https://docs.astral.sh/uv/), Node 24. Optional: [Ollama](https://ollama.com)
for local embeddings and generation.

## Run locally

From the repository root:

```powershell
Copy-Item .env.example .env       # bash: cp .env.example .env
docker compose up -d --wait db    # PostgreSQL + pgvector on localhost:5433

# Optional, for real local models. Without them set LLM_PROVIDER=extractive and
# EMBEDDING_PROVIDER=hash in .env: no model is used and answers are excerpts only.
ollama pull nomic-embed-text
ollama pull llama3

cd backend
uv sync --extra anthropic
uv run alembic upgrade head       # uses MIGRATION_DATABASE_URL, the owner role

# Create the first organization, workspace and owner. Prompts for a password (12+ characters).
uv run opsassist bootstrap --org "Acme" --org-slug acme `
  --workspace "Engineering" --workspace-slug engineering `
  --email you@example.com --name "Your Name"

uv run uvicorn opsassist.asgi:app --port 8000 --no-access-log   # terminal 1: API
uv run opsassist worker                         # terminal 2: ingestion worker
```

```powershell
cd web; npm ci; npm run dev                     # terminal 3: http://localhost:5173
```

Backend containers are also available: `docker compose --profile app up --build`. Those need a
`.env` file and connect to Ollama on the host. The container path builds, but only the local path
above has been exercised end to end.

**OneDrive note:** if the checkout is in a synced folder, keep the virtualenv outside it by setting
`UV_PROJECT_ENVIRONMENT`, for example `$env:UV_PROJECT_ENVIRONMENT="$env:LOCALAPPDATA\opsassist\venv"`.
`web/node_modules` and `backend/var` should be excluded from sync as well.

## Test

```powershell
cd backend
uv run ruff check . ; uv run ruff format --check . ; uv run mypy
uv run pytest                                   # requires the compose database
uv run python -m evals.run_rag_eval             # retrieval and permission metrics, hash embeddings
uv run python -m evals.run_rag_eval --embedding ollama --llm ollama --output evals/results/run.json

cd ..\web
npm run lint ; npm run typecheck ; npm test ; npm run build
# Browser test against a running local stack and a disposable account:
$env:E2E_EMAIL="..."; $env:E2E_PASSWORD="..."; npx playwright test
```

Tests connect as the non-owner application role, so row-level security is really exercised. They
use the `opsassist_test` database and never touch `opsassist`.

## Layout

```
backend/src/opsassist/
  api/          HTTP routes, middleware, error envelope. No business logic.
  identity/     Sessions, password hashing, roles, memberships, groups.
  knowledge/    Extraction, chunking, ingestion, ACL (access.py), hybrid retrieval.
  assistant/    Prompt assembly, citation validation, answer pipeline.
  llm/          Provider protocols and adapters (Ollama, Anthropic, hash).
  jobs/         Durable Postgres queue and worker.
  models.py     Schema. migrations/ holds the Alembic history.
backend/evals/  RAG evaluation corpus and runner.
backend/tests/  unit, integration, security (cross-tenant, cross-user, injection).
web/src/        React UI: Assistant, Knowledge, citation viewer.
docs/           Requirements, architecture, security model, integration strategy, plan, ADRs.
```

## Configuration

All settings come from environment variables, validated in `backend/src/opsassist/config.py`.
`.env.example` documents the main ones. External model providers refuse to start unless
`EXTERNAL_MODEL_DATA_APPROVED=true`; read `docs/SECURITY_MODEL.md` section 6 first.

## Operating notes

- Health: `GET /healthz` (process up), `GET /readyz` (database reachable and migrated).
- Logs are JSON on stdout with `request_id`. Document text and questions are not logged.
- Failed ingestion shows on the document in the Knowledge screen, and in `jobs.last_error`.
  Re-uploading the same file retries it.
- Changing the embedding model: documents embedded with the old model are no longer found by
  dense search (lexical still works) until re-uploaded. Bulk re-indexing is not built yet. A model
  with a dimension other than 768 needs a migration.
