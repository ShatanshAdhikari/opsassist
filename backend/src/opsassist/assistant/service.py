"""Conversation and question handling.

Each question is answered independently: earlier turns are not sent to the model. This keeps
content from since-revoked sources out of new prompts. Follow-up context is a later feature that
must respect that property.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.assistant.grounding import ValidatedAnswer, validate_answer
from opsassist.assistant.prompts import (
    NOT_FOUND_SENTINEL,
    SYSTEM_PROMPT,
    build_user_prompt,
    marker,
)
from opsassist.audit import record_event
from opsassist.container import Container
from opsassist.db import tenant_scope
from opsassist.errors import NotFound, ValidationFailed
from opsassist.knowledge.access import visible_source_ids
from opsassist.knowledge.retrieval import Evidence, RetrievalResult, retrieve
from opsassist.llm.base import GenerationResult, ProviderUnavailable
from opsassist.models import (
    Conversation,
    Document,
    DocumentVersion,
    Message,
    MessageRole,
    UsageRecord,
    VersionStatus,
    utcnow,
)
from opsassist.observability import get_logger, redact_secrets

log = get_logger("opsassist.assistant")

_EXCERPT_SNAPSHOT_CHARS = 1500
_NO_EVIDENCE_TEXT = (
    "No reliable source was found for this question in the sources you can access. "
    "No answer was generated."
)
_WITHHELD_TEXT = (
    "This answer is withheld because at least one of its sources was deleted or you no longer "
    "have access to it."
)


@dataclass(frozen=True)
class MessageView:
    message: Message
    content: str
    citations: list[dict[str, Any]]
    grounding: dict[str, Any] | None


async def create_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> Conversation:
    now = utcnow()
    conversation = Conversation(
        workspace_id=workspace_id, user_id=user_id, title=title, created_at=now, updated_at=now
    )
    session.add(conversation)
    return conversation


async def list_conversations(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> list[Conversation]:
    result = await session.scalars(
        select(Conversation)
        .where(Conversation.workspace_id == workspace_id, Conversation.user_id == user_id)
        .order_by(Conversation.updated_at.desc())
        .limit(100)
    )
    return list(result)


async def _owned_conversation(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """Conversations are private. Other users, including admins, get 404."""
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
            Conversation.user_id == user_id,
        )
    )
    if conversation is None:
        raise NotFound("Conversation not found")
    return conversation


async def list_messages(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[MessageView]:
    await _owned_conversation(session, workspace_id, user_id, conversation_id)
    messages = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.role.desc())
    )
    return await _views(session, workspace_id, user_id, list(messages))


async def _views(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID, messages: list[Message]
) -> list[MessageView]:
    """Re-check access on every read, since stored answers embed source content."""
    visible = await visible_source_ids(session, workspace_id, user_id)
    cited_documents = {
        uuid.UUID(citation["document_id"]) for message in messages for citation in message.citations
    }
    existing_documents: set[uuid.UUID] = set()
    if cited_documents:
        existing_documents = set(
            await session.scalars(select(Document.id).where(Document.id.in_(cited_documents)))
        )
    return [_view(message, visible, existing_documents) for message in messages]


def _view(
    message: Message, visible: set[uuid.UUID], existing_documents: set[uuid.UUID]
) -> MessageView:
    citations: list[dict[str, Any]] = []
    withheld = False
    for citation in message.citations:
        allowed = (
            uuid.UUID(citation["source_id"]) in visible
            and uuid.UUID(citation["document_id"]) in existing_documents
        )
        if allowed:
            citations.append(citation)
        else:
            withheld = True
            citations.append(
                {
                    "marker": citation["marker"],
                    "chunk_id": citation["chunk_id"],
                    "document_id": citation["document_id"],
                    "source_id": citation["source_id"],
                    "used": citation.get("used", True),
                    "access_revoked": True,
                }
            )
    grounding: dict[str, Any] | None = message.grounding or None
    if message.role != MessageRole.ASSISTANT:
        grounding = None
    if withheld:
        return MessageView(
            message, _WITHHELD_TEXT, citations, {**(grounding or {}), "status": "withheld"}
        )
    return MessageView(message, message.content, citations, grounding)


def _citation(index: int, item: Evidence, *, used: bool) -> dict[str, Any]:
    return {
        "marker": marker(index),
        "chunk_id": str(item.chunk_id),
        "document_id": str(item.document_id),
        "source_id": str(item.source_id),
        "document_title": item.document_title,
        "source_name": item.source_name,
        "version_number": item.version_number,
        "excerpt": redact_secrets(item.content)[:_EXCERPT_SNAPSHOT_CHARS],
        "section_path": list(item.section_path),
        "page": item.page,
        "line_start": item.line_start,
        "line_end": item.line_end,
        "indexed_at": item.indexed_at.isoformat(),
        "stale": item.stale,
        "used": used,
    }


def _excerpt_answer(evidence: list[Evidence], lead: str) -> str:
    lines = [lead]
    for index, item in enumerate(evidence):
        snippet = " ".join(redact_secrets(item.content).split())[:300]
        lines.append(f"- {item.document_title}: {snippet} [{marker(index)}]")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Outcome:
    content: str
    citations: list[dict[str, Any]]
    grounding: dict[str, Any]
    generation: GenerationResult | None
    latency_ms: float


async def _documents_being_indexed(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> int:
    visible = await visible_source_ids(session, workspace_id, user_id)
    count = await session.scalar(
        select(func.count())
        .select_from(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(
            DocumentVersion.status.in_([VersionStatus.PENDING, VersionStatus.PROCESSING]),
            Document.source_id.in_(visible),
        )
    )
    return count or 0


async def _answer(
    container: Container, question: str, result: RetrievalResult, indexing: int
) -> _Outcome:
    llm = container.llm
    evidence = result.evidence
    grounding: dict[str, Any] = {
        "provider": llm.name if llm else None,
        "model": llm.model if llm else None,
        "dense_available": result.dense_available,
        "uncited_sentences": 0,
        "removed_citations": [],
        "stale_sources": sum(1 for item in evidence if item.stale),
        "notes": [],
    }
    if not result.dense_available:
        grounding["notes"].append(
            "Semantic search was unavailable, so only keyword search was used."
        )
    if indexing:
        grounding["notes"].append(
            f"{indexing} document(s) are still being indexed and were not searched."
        )
    if grounding["stale_sources"]:
        grounding["notes"].append(
            "Some sources have not been updated recently and may be outdated."
        )

    if not evidence:
        # Never ask a model to answer with nothing to ground it.
        return _Outcome(_NO_EVIDENCE_TEXT, [], {**grounding, "status": "no_evidence"}, None, 0.0)

    all_cited = [_citation(i, item, used=True) for i, item in enumerate(evidence)]
    if llm is None:
        text = _excerpt_answer(evidence, "No model is configured. Most relevant excerpts:")
        return _Outcome(text, all_cited, {**grounding, "status": "extractive"}, None, 0.0)

    started = time.perf_counter()
    try:
        generation = await llm.generate(
            system=SYSTEM_PROMPT,
            user=build_user_prompt(question, evidence),
            max_tokens=container.settings.llm_max_output_tokens,
        )
    except ProviderUnavailable as exc:
        log.warning("model_unavailable", provider=llm.name, reason=str(exc))
        text = _excerpt_answer(
            evidence, "The model is unavailable, so no answer was generated. Relevant excerpts:"
        )
        latency = (time.perf_counter() - started) * 1000
        return _Outcome(
            text, all_cited, {**grounding, "status": "model_unavailable"}, None, latency
        )
    latency = (time.perf_counter() - started) * 1000

    valid = {marker(i) for i in range(len(evidence))}
    validated = validate_answer(generation.text, valid)
    return _finalize(validated, evidence, grounding, generation, latency)


def _finalize(
    validated: ValidatedAnswer,
    evidence: list[Evidence],
    grounding: dict[str, Any],
    generation: GenerationResult,
    latency: float,
) -> _Outcome:
    grounding = {
        **grounding,
        "uncited_sentences": validated.uncited_sentences,
        "removed_citations": list(validated.removed_markers),
    }
    if validated.removed_markers:
        grounding["notes"] = [
            *grounding["notes"],
            "The model cited sources that do not exist. Those citations were removed.",
        ]
    citations = [
        _citation(i, item, used=marker(i) in validated.used_markers)
        for i, item in enumerate(evidence)
    ]
    if validated.says_not_found:
        grounding["status"] = "no_evidence"
        return _Outcome(NOT_FOUND_SENTINEL, [], grounding, generation, latency)
    if not validated.used_markers:
        # An uncited generated answer is not evidence-backed. Show the excerpts, not the claims.
        grounding["status"] = "unsupported"
        grounding["notes"] = [
            *grounding["notes"],
            "The model's answer cited no sources, so it was discarded.",
        ]
        text = _excerpt_answer(
            evidence, "A verifiable answer could not be generated. Most relevant excerpts:"
        )
        all_cited = [_citation(i, item, used=True) for i, item in enumerate(evidence)]
        return _Outcome(text, all_cited, grounding, generation, latency)
    grounding["status"] = "partially_grounded" if validated.uncited_sentences else "grounded"
    if validated.uncited_sentences:
        grounding["notes"] = [
            *grounding["notes"],
            f"{validated.uncited_sentences} sentence(s) have no citation. Verify them.",
        ]
    return _Outcome(validated.text, citations, grounding, generation, latency)


async def ask(
    container: Container,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    question: str,
) -> tuple[MessageView, MessageView]:
    settings = container.settings
    if len(question) > settings.max_question_chars:
        raise ValidationFailed("Question is too long")
    limiter_key = f"{workspace_id}:{user_id}"
    container.question_limiter.check(limiter_key)
    container.question_limiter.hit(limiter_key)
    factory = container.session_factory

    # Embed before opening a transaction. On failure, fall back to lexical search and say so.
    query_embedding: list[float] | None = None
    try:
        query_embedding = await container.embedder.embed_query(question)
    except ProviderUnavailable as exc:
        log.warning("embedding_unavailable", reason=str(exc))

    async with tenant_scope(factory, workspace_id) as session:
        conversation = await _owned_conversation(session, workspace_id, user_id, conversation_id)
        now = utcnow()
        user_message = Message(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            role=MessageRole.USER,
            content=question,
            citations=[],
            grounding={},
            created_at=now,
        )
        session.add(user_message)
        conversation.updated_at = now
        result = await retrieve(
            session,
            settings=settings,
            workspace_id=workspace_id,
            user_id=user_id,
            query=question,
            query_embedding=query_embedding,
            embedding_model=container.embedder.model,
        )
        indexing = await _documents_being_indexed(session, workspace_id, user_id)

    # No transaction is open here, so a slow model does not hold a database connection.
    outcome = await _answer(container, question, result, indexing)

    async with tenant_scope(factory, workspace_id) as session:
        answer = Message(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT,
            content=outcome.content,
            citations=outcome.citations,
            grounding=outcome.grounding,
            created_at=utcnow(),
        )
        session.add(answer)
        await session.flush()
        if outcome.generation is not None and container.llm is not None:
            session.add(
                UsageRecord(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    message_id=answer.id,
                    provider=container.llm.name,
                    model=container.llm.model,
                    input_tokens=outcome.generation.input_tokens,
                    output_tokens=outcome.generation.output_tokens,
                    latency_ms=outcome.latency_ms,
                )
            )
        record_event(
            session,
            "knowledge.query",
            workspace_id=workspace_id,
            actor_user_id=user_id,
            target_type="message",
            target_id=answer.id,
            metadata={
                "status": outcome.grounding["status"],
                "provider": outcome.grounding["provider"],
                "model": outcome.grounding["model"],
                "chunks_retrieved": len(result.evidence),
                "source_ids": sorted({str(item.source_id) for item in result.evidence}),
                "dense_available": result.dense_available,
            },
        )
        views = await _views(session, workspace_id, user_id, [user_message, answer])
    return views[0], views[1]
