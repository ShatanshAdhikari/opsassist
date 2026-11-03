"""Knowledge sources, grants and documents."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.audit import record_event
from opsassist.config import Settings
from opsassist.errors import Conflict, NotFound, ValidationFailed
from opsassist.jobs import queue
from opsassist.knowledge.access import can_read_source, visible_source_ids
from opsassist.knowledge.blobs import BlobStore
from opsassist.knowledge.extraction import validate_upload
from opsassist.knowledge.ingestion import INGEST_JOB_KIND
from opsassist.models import (
    Chunk,
    Document,
    DocumentVersion,
    Group,
    KnowledgeSource,
    Membership,
    SourceGrant,
    SourceVisibility,
    VersionStatus,
    utcnow,
)


@dataclass(frozen=True)
class Actor:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    is_source_admin: bool = False


@dataclass(frozen=True)
class SourceView:
    source: KnowledgeSource
    document_count: int
    can_read: bool


@dataclass(frozen=True)
class DocumentView:
    document: Document
    latest: DocumentVersion
    current_version_number: int | None
    chunk_count: int
    indexed_at: datetime | None


async def create_source(
    session: AsyncSession, actor: Actor, name: str, description: str, visibility: SourceVisibility
) -> KnowledgeSource:
    source = KnowledgeSource(
        workspace_id=actor.workspace_id,
        name=name,
        description=description,
        kind="upload",
        visibility=visibility,
        created_by=actor.user_id,
    )
    session.add(source)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("A source with that name already exists") from exc
    if visibility == SourceVisibility.RESTRICTED:
        # The creator keeps access to what they restrict.
        session.add(
            SourceGrant(
                workspace_id=actor.workspace_id,
                source_id=source.id,
                user_id=actor.user_id,
                created_by=actor.user_id,
            )
        )
    record_event(
        session,
        "source.created",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="source",
        target_id=source.id,
        metadata={"visibility": visibility},
    )
    return source


async def list_sources(session: AsyncSession, actor: Actor) -> list[SourceView]:
    """Admins see every source's metadata in order to manage it. Content access is separate."""
    visible = await visible_source_ids(session, actor.workspace_id, actor.user_id)
    sources = await session.scalars(
        select(KnowledgeSource)
        .where(KnowledgeSource.workspace_id == actor.workspace_id)
        .order_by(KnowledgeSource.name)
    )
    count_rows = await session.execute(
        select(Document.source_id, func.count())
        .where(Document.workspace_id == actor.workspace_id)
        .group_by(Document.source_id)
    )
    counts = {row[0]: row[1] for row in count_rows}
    return [
        SourceView(source, counts.get(source.id, 0), source.id in visible)
        for source in sources
        if actor.is_source_admin or source.id in visible
    ]


async def _get_source(session: AsyncSession, actor: Actor, source_id: uuid.UUID) -> KnowledgeSource:
    source = await session.scalar(
        select(KnowledgeSource).where(
            KnowledgeSource.id == source_id, KnowledgeSource.workspace_id == actor.workspace_id
        )
    )
    if source is None:
        raise NotFound("Source not found")
    return source


async def _get_readable_source(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> KnowledgeSource:
    source = await _get_source(session, actor, source_id)
    if not await can_read_source(session, actor.workspace_id, actor.user_id, source_id):
        raise NotFound("Source not found")
    return source


async def _get_admin_source(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> KnowledgeSource:
    """Admin operations act on metadata and do not reveal content, so they need no grant."""
    return await _get_source(session, actor, source_id)


async def delete_source(session: AsyncSession, actor: Actor, source_id: uuid.UUID) -> list[str]:
    """Delete a source and return blob keys to remove after commit."""
    source = await _get_admin_source(session, actor, source_id)
    keys = list(
        await session.scalars(
            select(DocumentVersion.blob_key)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.source_id == source_id)
        )
    )
    await session.delete(source)
    record_event(
        session,
        "source.deleted",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="source",
        target_id=source_id,
        metadata={"name": source.name, "blobs": len(keys)},
    )
    return keys


async def list_grants(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> list[SourceGrant]:
    await _get_admin_source(session, actor, source_id)
    result = await session.scalars(select(SourceGrant).where(SourceGrant.source_id == source_id))
    return list(result)


async def create_grant(
    session: AsyncSession,
    actor: Actor,
    source_id: uuid.UUID,
    user_id: uuid.UUID | None,
    group_id: uuid.UUID | None,
) -> SourceGrant:
    await _get_admin_source(session, actor, source_id)
    if (user_id is None) == (group_id is None):
        raise ValidationFailed("Provide exactly one of user_id or group_id")
    if user_id is not None:
        member = await session.scalar(
            select(Membership).where(
                Membership.workspace_id == actor.workspace_id, Membership.user_id == user_id
            )
        )
        if member is None:
            raise NotFound("User is not a member of this workspace")
    else:
        group = await session.scalar(
            select(Group).where(Group.id == group_id, Group.workspace_id == actor.workspace_id)
        )
        if group is None:
            raise NotFound("Group not found")
    grant = SourceGrant(
        workspace_id=actor.workspace_id,
        source_id=source_id,
        user_id=user_id,
        group_id=group_id,
        created_by=actor.user_id,
    )
    session.add(grant)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict("That grant already exists") from exc
    record_event(
        session,
        "source.grant_created",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="source",
        target_id=source_id,
        metadata={
            "user_id": str(user_id) if user_id else None,
            "group_id": str(group_id) if group_id else None,
        },
    )
    return grant


async def delete_grant(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID, grant_id: uuid.UUID
) -> None:
    grant = await session.scalar(
        select(SourceGrant).where(SourceGrant.id == grant_id, SourceGrant.source_id == source_id)
    )
    if grant is None:
        raise NotFound("Grant not found")
    await session.delete(grant)
    record_event(
        session,
        "source.grant_revoked",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="source",
        target_id=source_id,
        metadata={
            "user_id": str(grant.user_id) if grant.user_id else None,
            "group_id": str(grant.group_id) if grant.group_id else None,
        },
    )


async def upload_document(
    session: AsyncSession,
    actor: Actor,
    settings: Settings,
    blobs: BlobStore,
    source_id: uuid.UUID,
    filename: str,
    data: bytes,
) -> tuple[Document, DocumentVersion, bool]:
    """Create a document or new version and queue ingestion. Return (doc, version, deduplicated).

    Identical content to the latest version is not reprocessed unless the last attempt failed.
    """
    await _get_readable_source(session, actor, source_id)
    upload = validate_upload(filename, data, max_bytes=settings.max_upload_bytes)
    content_hash = hashlib.sha256(data).hexdigest()

    document = await session.scalar(
        select(Document)
        .where(Document.source_id == source_id, Document.external_id == upload.filename)
        .with_for_update()
    )
    if document is None:
        document = Document(
            workspace_id=actor.workspace_id,
            source_id=source_id,
            external_id=upload.filename,
            title=upload.title,
            content_type=upload.content_type,
            created_by=actor.user_id,
        )
        session.add(document)
        try:
            await session.flush()
        except IntegrityError as exc:
            raise Conflict("That document is being uploaded concurrently. Retry.") from exc
        latest = None
    else:
        latest = await session.scalar(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == document.id)
            .order_by(DocumentVersion.version_number.desc())
            .limit(1)
        )

    if latest is not None and latest.content_hash == content_hash:
        if latest.status == VersionStatus.FAILED:
            latest.status = VersionStatus.PENDING
            latest.error = None
            _enqueue_ingest(session, actor, settings, latest)
            return document, latest, False
        return document, latest, True

    version = DocumentVersion(
        workspace_id=actor.workspace_id,
        document_id=document.id,
        version_number=(latest.version_number + 1) if latest else 1,
        content_hash=content_hash,
        blob_key=f"{actor.workspace_id}/{uuid.uuid4()}",
        byte_size=len(data),
        status=VersionStatus.PENDING,
        chunk_count=0,
    )
    session.add(version)
    await session.flush()
    # Written inside the transaction, so a failed commit leaves an orphan blob, never a
    # version row pointing at nothing. Orphans are harmless but need a future sweep.
    await blobs.put(version.blob_key, data)
    _enqueue_ingest(session, actor, settings, version)
    record_event(
        session,
        "document.uploaded",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="document",
        target_id=document.id,
        metadata={
            "version": version.version_number,
            "bytes": len(data),
            "content_hash": content_hash,
        },
    )
    return document, version, False


def _enqueue_ingest(
    session: AsyncSession, actor: Actor, settings: Settings, version: DocumentVersion
) -> None:
    queue.enqueue(
        session,
        workspace_id=actor.workspace_id,
        kind=INGEST_JOB_KIND,
        payload={"version_id": str(version.id)},
        max_attempts=settings.job_max_attempts,
    )


async def list_documents(
    session: AsyncSession, actor: Actor, source_id: uuid.UUID
) -> list[DocumentView]:
    await _get_readable_source(session, actor, source_id)
    documents = list(
        await session.scalars(
            select(Document).where(Document.source_id == source_id).order_by(Document.title)
        )
    )
    return [await document_view(session, document) for document in documents]


async def document_view(session: AsyncSession, document: Document) -> DocumentView:
    latest = await session.scalar(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version_number.desc())
        .limit(1)
    )
    if latest is None:
        raise NotFound("Document has no versions")
    current = None
    if document.current_version_id is not None:
        current = await session.scalar(
            select(DocumentVersion).where(DocumentVersion.id == document.current_version_id)
        )
    return DocumentView(
        document=document,
        latest=latest,
        current_version_number=current.version_number if current else None,
        chunk_count=current.chunk_count if current else 0,
        indexed_at=current.indexed_at if current else None,
    )


async def delete_document(session: AsyncSession, actor: Actor, document_id: uuid.UUID) -> list[str]:
    """Delete a document with its chunks. Return blob keys to remove after commit."""
    document = await session.scalar(
        select(Document)
        .where(Document.id == document_id, Document.workspace_id == actor.workspace_id)
        .with_for_update()
    )
    if document is None or not await can_read_source(
        session, actor.workspace_id, actor.user_id, document.source_id
    ):
        raise NotFound("Document not found")
    keys = list(
        await session.scalars(
            select(DocumentVersion.blob_key).where(DocumentVersion.document_id == document_id)
        )
    )
    await session.delete(document)
    record_event(
        session,
        "document.deleted",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="document",
        target_id=document_id,
        metadata={"title": document.title, "versions": len(keys)},
    )
    return keys


async def get_chunk(
    session: AsyncSession, actor: Actor, chunk_id: uuid.UUID
) -> tuple[Chunk, Document, KnowledgeSource, int]:
    """Source inspection for citations. Enforces the same access check as retrieval."""
    row = (
        await session.execute(
            select(Chunk, Document, KnowledgeSource, DocumentVersion.version_number)
            .join(Document, Document.id == Chunk.document_id)
            .join(KnowledgeSource, KnowledgeSource.id == Chunk.source_id)
            .join(DocumentVersion, DocumentVersion.id == Chunk.version_id)
            .where(Chunk.id == chunk_id, Chunk.workspace_id == actor.workspace_id)
        )
    ).first()
    if row is None:
        raise NotFound("Chunk not found")
    chunk, document, source, version_number = row
    if not await can_read_source(session, actor.workspace_id, actor.user_id, source.id):
        raise NotFound("Chunk not found")
    record_event(
        session,
        "knowledge.chunk_viewed",
        workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        target_type="chunk",
        target_id=chunk_id,
        metadata={"source_id": str(source.id), "viewed_at": utcnow().isoformat()},
    )
    return chunk, document, source, version_number
