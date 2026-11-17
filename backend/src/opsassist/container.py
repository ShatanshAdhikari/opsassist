"""Composition root: long-lived services shared by the API and the worker."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from opsassist.config import Settings
from opsassist.credentials.crypto import KeyRing
from opsassist.credentials.store import CredentialStore
from opsassist.db import SessionFactory, create_engine, create_session_factory
from opsassist.identity.service import AuthService, SlidingWindowLimiter
from opsassist.integrations.github_client import InstallationTokens
from opsassist.integrations.github_provider import GitHubDeps
from opsassist.integrations.gmail_client import AccessTokens
from opsassist.integrations.gmail_provider import GmailDeps
from opsassist.knowledge.blobs import BlobStore, LocalBlobStore
from opsassist.llm.base import EmbeddingProvider, LLMProvider
from opsassist.llm.factory import build_embedding_provider, build_llm_provider
from opsassist.tools.factory import build_provider_factory
from opsassist.tools.gateway import ToolGateway


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    session_factory: SessionFactory
    auth: AuthService
    blobs: BlobStore
    embedder: EmbeddingProvider
    llm: LLMProvider | None
    question_limiter: SlidingWindowLimiter
    tools: ToolGateway
    credentials: CredentialStore
    github: GitHubDeps
    gmail: GmailDeps

    async def close(self) -> None:
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    credentials = CredentialStore(
        KeyRing(settings.credential_keys, settings.credential_active_key_id)
    )
    github = GitHubDeps(settings, factory, credentials, InstallationTokens())
    gmail = GmailDeps(settings, factory, credentials, AccessTokens())
    return Container(
        settings=settings,
        engine=engine,
        session_factory=factory,
        auth=AuthService(settings, factory),
        blobs=LocalBlobStore(settings.blob_dir),
        embedder=build_embedding_provider(settings),
        llm=build_llm_provider(settings),
        question_limiter=SlidingWindowLimiter(settings.questions_per_minute, 60),
        tools=ToolGateway(settings, factory, build_provider_factory(settings, github, gmail)),
        credentials=credentials,
        github=github,
        gmail=gmail,
    )
