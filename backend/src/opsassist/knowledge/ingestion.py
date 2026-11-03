"""Ingestion job: extract, chunk, embed, then activate the version in one transaction.

Idempotent, since the queue is at-least-once. Readers keep seeing the previous version until the
final transaction commits. The slow steps run outside any database transaction.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from sqlalchemy import delete, select

from opsassist.audit import record_event
from opsassist.config import Settings
from opsassist.db import SessionFactory, tenant_scope
from opsassist.jobs.worker import PermanentJobError
from opsassist.knowledge.blobs import BlobStore
from opsassist.knowledge.chunking import ChunkDraft, chunk_document
from opsassist.knowledge.extraction import ExtractionError, extract
from opsassist.llm.base import EmbeddingProvider
from opsassist.models import Chunk, Document, DocumentVersion, Job, VersionStatus, utcnow
from opsassist.observability import get_logger

INGEST_JOB_KIND = "ingest_document_version"

log = get_logger("opsassist.ingestion")


def contextual_text(title: str, draft: ChunkDraft) -> str:
    """Text used for embedding and lexical search: the chunk plus where it sits."""
    parts = [title]
    if draft.section_path:
        parts.append(" > ".join(draft.section_path))
    parts.append(draft.content)
    return "\n".join(parts)


class IngestHandler:
    def __init__(
        self,
        settings: Settings,
        factory: SessionFactory,
        blobs: BlobStore,
        embedder: EmbeddingProvider,
    ) -> None:
        self._settings = settings
        self._factory = factory
        self._blobs = blobs
        self._embedder = embedder

    async def run(self, job: Job) -> None:
        version_id = uuid.UUID(job.payload["version_id"])
        workspace_id = job.workspace_id

        async with tenant_scope(self._factory, workspace_id) as session:
            row = (
                await session.execute(
                    select(DocumentVersion, Document)
                    .join(Document, Document.id == DocumentVersion.document_id)
                    .where(DocumentVersion.id == version_id)
                )
            ).first()
            if row is None:
                log.info("ingest_target_deleted")  # Deleted while queued: nothing to do.
                return
            version, document = row
            if version.status == VersionStatus.READY:
                return
            version.status = VersionStatus.PROCESSING
            blob_key, title, content_type = version.blob_key, document.title, document.content_type

        data = await self._blobs.get(blob_key)
        settings = self._settings
        try:
            extracted = await asyncio.to_thread(
                extract,
                content_type,
                data,
                max_pages=settings.max_pdf_pages,
                max_chars=settings.max_extracted_chars,
            )
        except ExtractionError as exc:
            raise PermanentJobError(str(exc)) from exc

        drafts = await asyncio.to_thread(chunk_document, content_type, extracted)
        if not drafts:
            raise PermanentJobError("Document produced no searchable content")
        texts = [contextual_text(title, draft) for draft in drafts]
        embeddings = await self._embedder.embed_documents(texts)

        await self._activate(workspace_id, version_id, drafts, texts, embeddings)

    async def _activate(
        self,
        workspace_id: uuid.UUID,
        version_id: uuid.UUID,
        drafts: list[ChunkDraft],
        texts: list[str],
        embeddings: list[list[float]],
    ) -> None:
        async with tenant_scope(self._factory, workspace_id) as session:
            version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version_id)
            )
            if version is None:
                return
            # The document lock serializes activation with concurrent versions and deletion.
            document = await session.scalar(
                select(Document).where(Document.id == version.document_id).with_for_update()
            )
            if document is None:
                return
            await session.execute(delete(Chunk).where(Chunk.version_id == version_id))
            session.add_all(
                Chunk(
                    workspace_id=workspace_id,
                    source_id=document.source_id,
                    document_id=document.id,
                    version_id=version_id,
                    ordinal=ordinal,
                    kind=draft.kind,
                    content=draft.content,
                    content_hash=hashlib.sha256(draft.content.encode()).hexdigest(),
                    section_path=list(draft.section_path),
                    page=draft.page,
                    line_start=draft.line_start,
                    line_end=draft.line_end,
                    embedding=embedding,
                    embedding_model=self._embedder.model,
                    lexical_text=lexical_text,
                )
                for ordinal, (draft, lexical_text, embedding) in enumerate(
                    zip(drafts, texts, embeddings, strict=True)
                )
            )
            now = utcnow()
            version.status = VersionStatus.READY
            version.error = None
            version.chunk_count = len(drafts)
            version.embedding_model = self._embedder.model
            version.indexed_at = now

            current_number = await session.scalar(
                select(DocumentVersion.version_number).where(
                    DocumentVersion.id == document.current_version_id
                )
            )
            # An older version finishing late must not replace a newer one.
            # Equal means a redelivered job re-indexing the active version, which stays active.
            if current_number is None or version.version_number >= current_number:
                await session.flush()
                if document.current_version_id not in (None, version_id):
                    await session.execute(
                        delete(Chunk).where(Chunk.version_id == document.current_version_id)
                    )
                document.current_version_id = version_id
                document.updated_at = now
            else:
                await session.execute(delete(Chunk).where(Chunk.version_id == version_id))
            record_event(
                session,
                "document.indexed",
                workspace_id=workspace_id,
                target_type="document_version",
                target_id=version_id,
                metadata={
                    "document_id": str(document.id),
                    "chunks": len(drafts),
                    "embedding_model": self._embedder.model,
                },
            )

    async def on_dead(self, job: Job, public_message: str) -> None:
        version_id = uuid.UUID(job.payload["version_id"])
        async with tenant_scope(self._factory, job.workspace_id) as session:
            version = await session.scalar(
                select(DocumentVersion).where(DocumentVersion.id == version_id)
            )
            if version is None:
                return
            version.status = VersionStatus.FAILED
            version.error = public_message[:1000]
            record_event(
                session,
                "document.index_failed",
                workspace_id=job.workspace_id,
                target_type="document_version",
                target_id=version_id,
                outcome="failure",
                metadata={"attempts": job.attempts},
            )
