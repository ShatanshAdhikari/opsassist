"""Knowledge sources, grants, documents, search and source inspection."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Request, UploadFile, status

from opsassist.api.deps import WorkspaceContext, WorkspaceDep
from opsassist.api.schemas import (
    ChunkOut,
    CreateGrantRequest,
    CreateSourceRequest,
    DocumentOut,
    GrantOut,
    SearchHit,
    SearchOut,
    SearchRequest,
    SourceOut,
    UploadOut,
)
from opsassist.errors import PayloadTooLarge, ValidationFailed
from opsassist.identity.permissions import Permission, has_permission
from opsassist.knowledge import service
from opsassist.knowledge.retrieval import retrieve
from opsassist.llm.base import ProviderUnavailable
from opsassist.models import SourceVisibility
from opsassist.observability import get_logger, redact_secrets

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["knowledge"])
log = get_logger("opsassist.knowledge")

_READ_CHUNK = 1024 * 1024
_SEARCH_EXCERPT_CHARS = 600


def _actor(ctx: WorkspaceContext) -> service.Actor:
    return service.Actor(
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        is_source_admin=has_permission(ctx.role, Permission.SOURCES_ADMIN),
    )


def _source_out(view: service.SourceView) -> SourceOut:
    source = view.source
    return SourceOut(
        id=source.id,
        name=source.name,
        description=source.description,
        kind=source.kind,
        visibility=SourceVisibility(source.visibility),
        created_at=source.created_at,
        document_count=view.document_count,
        can_read=view.can_read,
    )


def _document_out(view: service.DocumentView) -> DocumentOut:
    return DocumentOut(
        id=view.document.id,
        source_id=view.document.source_id,
        title=view.document.title,
        content_type=view.document.content_type,
        updated_at=view.document.updated_at,
        current_version_number=view.current_version_number,
        latest_version_number=view.latest.version_number,
        latest_status=view.latest.status,
        latest_error=view.latest.error,
        chunk_count=view.chunk_count,
        indexed_at=view.indexed_at,
    )


@router.get("/sources")
async def list_sources(ctx: WorkspaceDep) -> list[SourceOut]:
    ctx.require(Permission.WORKSPACE_READ)
    async with ctx.tx() as session:
        return [_source_out(view) for view in await service.list_sources(session, _actor(ctx))]


@router.post("/sources", status_code=status.HTTP_201_CREATED)
async def create_source(body: CreateSourceRequest, ctx: WorkspaceDep) -> SourceOut:
    ctx.require(Permission.SOURCES_CREATE)
    async with ctx.tx() as session:
        source = await service.create_source(
            session, _actor(ctx), body.name, body.description, body.visibility
        )
    return _source_out(service.SourceView(source, 0, True))


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(source_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    ctx.require(Permission.SOURCES_ADMIN)
    async with ctx.tx() as session:
        keys = await service.delete_source(session, _actor(ctx), source_id)
    await _remove_blobs(ctx, keys)


@router.get("/sources/{source_id}/grants")
async def list_grants(source_id: uuid.UUID, ctx: WorkspaceDep) -> list[GrantOut]:
    ctx.require(Permission.SOURCES_ADMIN)
    async with ctx.tx() as session:
        grants = await service.list_grants(session, _actor(ctx), source_id)
    return [GrantOut.model_validate(grant) for grant in grants]


@router.post("/sources/{source_id}/grants", status_code=status.HTTP_201_CREATED)
async def create_grant(
    source_id: uuid.UUID, body: CreateGrantRequest, ctx: WorkspaceDep
) -> GrantOut:
    ctx.require(Permission.SOURCES_ADMIN)
    async with ctx.tx() as session:
        grant = await service.create_grant(
            session, _actor(ctx), source_id, body.user_id, body.group_id
        )
    return GrantOut.model_validate(grant)


@router.delete("/sources/{source_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_grant(source_id: uuid.UUID, grant_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    ctx.require(Permission.SOURCES_ADMIN)
    async with ctx.tx() as session:
        await service.delete_grant(session, _actor(ctx), source_id, grant_id)


@router.get("/sources/{source_id}/documents")
async def list_documents(source_id: uuid.UUID, ctx: WorkspaceDep) -> list[DocumentOut]:
    ctx.require(Permission.WORKSPACE_READ)
    async with ctx.tx() as session:
        views = await service.list_documents(session, _actor(ctx), source_id)
    return [_document_out(view) for view in views]


@router.post("/sources/{source_id}/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    source_id: uuid.UUID,
    request: Request,
    ctx: WorkspaceDep,
    file: Annotated[UploadFile, File()],
) -> UploadOut:
    ctx.require(Permission.DOCUMENTS_WRITE)
    settings = ctx.container.settings
    declared = request.headers.get("content-length")
    # Multipart overhead is small. Reject clearly oversized bodies before reading them.
    if declared and declared.isdigit() and int(declared) > settings.max_upload_bytes + 1_000_000:
        raise PayloadTooLarge(f"File exceeds the {settings.max_upload_bytes} byte limit")
    buffer = bytearray()
    while chunk := await file.read(_READ_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > settings.max_upload_bytes:
            raise PayloadTooLarge(f"File exceeds the {settings.max_upload_bytes} byte limit")
    if not file.filename:
        raise ValidationFailed("Upload has no filename")
    async with ctx.tx() as session:
        document, _version, deduplicated = await service.upload_document(
            session,
            _actor(ctx),
            settings,
            ctx.container.blobs,
            source_id,
            file.filename,
            bytes(buffer),
        )
        view = await service.document_view(session, document)
    return UploadOut(document=_document_out(view), deduplicated=deduplicated)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: uuid.UUID, ctx: WorkspaceDep) -> None:
    ctx.require(Permission.DOCUMENTS_WRITE)
    async with ctx.tx() as session:
        keys = await service.delete_document(session, _actor(ctx), document_id)
    await _remove_blobs(ctx, keys)


async def _remove_blobs(ctx: WorkspaceContext, keys: list[str]) -> None:
    """Runs after commit. A failure leaves an unreferenced file, logged for cleanup."""
    for key in keys:
        try:
            await ctx.container.blobs.delete(key)
        except OSError as exc:
            log.error("blob_delete_failed", blob_key=key, error_type=type(exc).__name__)


@router.get("/chunks/{chunk_id}")
async def get_chunk(chunk_id: uuid.UUID, ctx: WorkspaceDep) -> ChunkOut:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    async with ctx.tx() as session:
        chunk, document, source, version_number = await service.get_chunk(
            session, _actor(ctx), chunk_id
        )
    return ChunkOut(
        id=chunk.id,
        document_id=document.id,
        document_title=document.title,
        source_name=source.name,
        version_number=version_number,
        content=chunk.content,
        section_path=chunk.section_path,
        page=chunk.page,
        line_start=chunk.line_start,
        line_end=chunk.line_end,
    )


@router.post("/search")
async def search(body: SearchRequest, ctx: WorkspaceDep) -> SearchOut:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    container = ctx.container
    embedding: list[float] | None = None
    try:
        embedding = await container.embedder.embed_query(body.query)
    except ProviderUnavailable as exc:
        log.warning("embedding_unavailable", reason=str(exc))
    async with ctx.tx() as session:
        result = await retrieve(
            session,
            settings=container.settings,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            query=body.query,
            query_embedding=embedding,
            embedding_model=container.embedder.model,
            top_k=body.limit,
        )
    return SearchOut(
        dense_available=result.dense_available,
        hits=[
            SearchHit(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                document_title=item.document_title,
                source_name=item.source_name,
                version_number=item.version_number,
                excerpt=redact_secrets(item.content)[:_SEARCH_EXCERPT_CHARS],
                section_path=list(item.section_path),
                page=item.page,
                line_start=item.line_start,
                line_end=item.line_end,
                score=item.score,
                matched_by=list(item.matched_by),
                indexed_at=item.indexed_at,
                stale=item.stale,
            )
            for item in result.evidence
        ],
    )
