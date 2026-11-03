"""Shared fixtures. Integration tests use a real PostgreSQL (docker compose up -d db).

The application connects as the non-owner app role, so RLS is exercised exactly as in production.
Fixture setup uses the owner role only to migrate and truncate.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from opsassist.api.app import create_app
from opsassist.config import Settings
from opsassist.container import Container, build_container
from opsassist.db import global_scope, tenant_scope
from opsassist.identity.service import SlidingWindowLimiter, hash_password
from opsassist.jobs.worker import Worker
from opsassist.knowledge.ingestion import INGEST_JOB_KIND, IngestHandler
from opsassist.llm.base import GenerationResult, ProviderUnavailable
from opsassist.models import Membership, Organization, Role, User, Workspace

BACKEND_DIR = Path(__file__).resolve().parents[1]
OWNER_URL = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://opsassist_owner:dev_owner_password@localhost:5433/opsassist_test",
)
APP_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opsassist_app:dev_app_password@localhost:5433/opsassist_test",
)
ORIGIN = "http://localhost:5173"
PASSWORD = "correct-horse-battery-staple"
_PASSWORD_HASH = hash_password(PASSWORD)

_TRUNCATE = """
    TRUNCATE organizations, users, sessions, audit_events, jobs, workspaces, memberships, groups,
    group_members, knowledge_sources, source_grants, documents, document_versions, chunks,
    conversations, messages, usage_records, tool_servers, tool_definitions, tool_invocations CASCADE
"""


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "environment": "test",
        "database_url": APP_URL,
        "cookie_secure": False,
        "allowed_origins": [ORIGIN],
        "blob_dir": tmp_path / "blobs",
        "llm_provider": "extractive",
        "embedding_provider": "hash",
        "login_max_failures": 5,
        "job_backoff_base_seconds": 0.0,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def migrated_database() -> None:
    environment = {**os.environ, "MIGRATION_DATABASE_URL": OWNER_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=environment,
        check=True,
        capture_output=True,
    )


@pytest_asyncio.fixture
async def clean_database(migrated_database: None) -> None:
    engine = create_async_engine(OWNER_URL)
    async with engine.begin() as connection:
        await connection.execute(text(_TRUNCATE))
    await engine.dispose()


@pytest_asyncio.fixture
async def owner_engine(migrated_database: None) -> AsyncIterator[object]:
    engine = create_async_engine(OWNER_URL)
    yield engine
    await engine.dispose()


class FakeLLM:
    """Scriptable model. Records prompts so tests can inspect exactly what reached it."""

    name = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.response = "The answer is documented [S1]."
        self.error: Exception | None = None

    async def generate(self, *, system: str, user: str, max_tokens: int) -> GenerationResult:
        self.prompts.append(user)
        if self.error:
            raise self.error
        return GenerationResult(self.response, 10, 5)


class FailingEmbedder:
    name = "failing"
    model = "hash-v1"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise ProviderUnavailable("embedding outage")

    async def embed_query(self, text: str) -> list[float]:
        raise ProviderUnavailable("embedding outage")


@dataclass
class Tenant:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    email: str


class Harness:
    """Application under test plus helpers for arranging tenants and clients."""

    def __init__(self, container: Container) -> None:
        self.container = container
        self.app = create_app(container=container)
        self._clients: list[httpx.AsyncClient] = []

    def client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"Origin": ORIGIN},
        )
        self._clients.append(client)
        return client

    async def close(self) -> None:
        for client in self._clients:
            await client.aclose()
        await self.container.close()

    async def create_user(self, email: str | None = None) -> tuple[uuid.UUID, str]:
        email = email or f"{uuid.uuid4().hex[:10]}@example.test"
        async with global_scope(self.container.session_factory) as session:
            user = User(email=email, display_name=email, password_hash=_PASSWORD_HASH)
            session.add(user)
            await session.flush()
            return user.id, email

    async def create_workspace(self) -> uuid.UUID:
        slug = uuid.uuid4().hex[:10]
        async with global_scope(self.container.session_factory) as session:
            organization = Organization(name=slug, slug=slug)
            session.add(organization)
            await session.flush()
            workspace = Workspace(organization_id=organization.id, name=slug, slug=slug)
            session.add(workspace)
            await session.flush()
            return workspace.id

    async def add_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID, role: Role) -> None:
        async with global_scope(self.container.session_factory) as session:
            session.add(Membership(workspace_id=workspace_id, user_id=user_id, role=role))

    async def tenant(self, role: Role = Role.OWNER) -> Tenant:
        user_id, email = await self.create_user()
        workspace_id = await self.create_workspace()
        await self.add_member(workspace_id, user_id, role)
        return Tenant(user_id, workspace_id, email)

    async def member_of(self, workspace_id: uuid.UUID, role: Role = Role.MEMBER) -> Tenant:
        user_id, email = await self.create_user()
        await self.add_member(workspace_id, user_id, role)
        return Tenant(user_id, workspace_id, email)

    async def login(self, email: str) -> httpx.AsyncClient:
        client = self.client()
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        return client

    def worker(self) -> Worker:
        container = self.container
        handler = IngestHandler(
            container.settings, container.session_factory, container.blobs, container.embedder
        )
        return Worker(
            container.settings,
            container.session_factory,
            {INGEST_JOB_KIND: handler},
            worker_id="test-worker",
        )

    async def drain_jobs(self) -> int:
        """Run jobs until none is claimable. Retried jobs run again immediately in tests."""
        worker = self.worker()
        processed = 0
        while await worker.run_once():
            processed += 1
        return processed

    async def create_source(
        self, client: httpx.AsyncClient, workspace_id: uuid.UUID, **body: object
    ) -> dict:
        body.setdefault("name", f"source-{uuid.uuid4().hex[:8]}")
        response = await client.post(f"/api/v1/workspaces/{workspace_id}/sources", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    async def upload(
        self,
        client: httpx.AsyncClient,
        workspace_id: uuid.UUID,
        source_id: str,
        filename: str,
        content: bytes | str,
        *,
        ingest: bool = True,
    ) -> dict:
        data = content.encode() if isinstance(content, str) else content
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/sources/{source_id}/documents",
            files={"file": (filename, data)},
        )
        assert response.status_code == 202, response.text
        if ingest:
            await self.drain_jobs()
        return response.json()

    async def conversation(self, client: httpx.AsyncClient, workspace_id: uuid.UUID) -> str:
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations", json={"title": "Test"}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def ask(
        self,
        client: httpx.AsyncClient,
        workspace_id: uuid.UUID,
        question: str,
        conversation_id: str | None = None,
    ) -> dict:
        conversation_id = conversation_id or await self.conversation(client, workspace_id)
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/conversations/{conversation_id}/messages",
            json={"question": question},
        )
        assert response.status_code == 200, response.text
        return response.json()["answer"]


@pytest_asyncio.fixture
async def harness(clean_database: None, tmp_path: Path) -> AsyncIterator[Harness]:
    container = build_container(make_settings(tmp_path))
    harness = Harness(container)
    yield harness
    await harness.close()


@pytest_asyncio.fixture
async def llm_harness(harness: Harness) -> FakeLLM:
    """Attach a scriptable model to the harness."""
    fake = FakeLLM()
    harness.container.llm = fake
    return fake


__all__ = ["FailingEmbedder", "FakeLLM", "Harness", "SlidingWindowLimiter", "tenant_scope"]
