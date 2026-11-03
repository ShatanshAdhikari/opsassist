"""Composition root: long-lived services shared by the API and the worker."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from opsassist.config import Settings
from opsassist.db import SessionFactory, create_engine, create_session_factory
from opsassist.identity.service import AuthService, SlidingWindowLimiter
from opsassist.knowledge.blobs import BlobStore, LocalBlobStore
from opsassist.llm.base import EmbeddingProvider, LLMProvider
from opsassist.llm.factory import build_embedding_provider, build_llm_provider


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

    async def close(self) -> None:
        await self.engine.dispose()


def build_container(settings: Settings) -> Container:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    return Container(
        settings=settings,
        engine=engine,
        session_factory=factory,
        auth=AuthService(settings, factory),
        blobs=LocalBlobStore(settings.blob_dir),
        embedder=build_embedding_provider(settings),
        llm=build_llm_provider(settings),
        question_limiter=SlidingWindowLimiter(settings.questions_per_minute, 60),
    )
