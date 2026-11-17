"""Conversation and question handling.

One question runs as a fixed sequence, not a loop: plan once, run the planned read-only calls,
retrieve documents, answer once. Limits on time, tokens and calls apply across all of it (ADR-0013).

Follow-up context has two parts. Planning sees only the user's earlier questions. Answering also
sees earlier answers, but only those that pass the same access check as reading them, and never as
evidence. This keeps content from since-revoked sources out of new prompts.

The sequence runs inside the request and is not durable: a crash loses the answer, leaving the
question stored. That is acceptable only because every call is a read, so asking again is safe.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.assistant.grounding import ValidatedAnswer, validate_answer
from opsassist.assistant.planner import (
    PLANNER_SYSTEM_PROMPT,
    Catalog,
    Plan,
    PlanRejected,
    build_planner_prompt,
    load_catalog,
    parse_plan,
)
from opsassist.assistant.prompts import (
    NOT_FOUND_SENTINEL,
    SYSTEM_PROMPT,
    HistoryTurn,
    build_user_prompt,
    marker,
)
from opsassist.assistant.tool_runner import ToolCallRecord, ToolEvidence, run_calls
from opsassist.audit import record_event
from opsassist.container import Container
from opsassist.db import tenant_scope
from opsassist.errors import NotFound, ValidationFailed
from opsassist.identity.permissions import Permission, has_permission
from opsassist.knowledge.access import visible_source_ids
from opsassist.knowledge.retrieval import Evidence, RetrievalResult, retrieve
from opsassist.llm.base import GenerationResult, LLMProvider, ProviderUnavailable
from opsassist.models import (
    Conversation,
    Document,
    DocumentVersion,
    Message,
    MessageRole,
    Role,
    ToolServer,
    UsageRecord,
    VersionStatus,
    utcnow,
)
from opsassist.observability import get_logger, redact_secrets
from opsassist.tools.base import InvocationStatus
from opsassist.tools.gateway import ToolActor

log = get_logger("opsassist.assistant")

_EXCERPT_SNAPSHOT_CHARS = 1500
_MIN_ANSWER_TOKENS = 256
_CHARS_PER_TOKEN_ESTIMATE = 4
_NO_EVIDENCE_TEXT = (
    "No reliable source was found for this question in the sources you can access. "
    "No answer was generated."
)
_WITHHELD_TEXT = (
    "This answer is withheld because at least one of its sources was deleted or you no longer "
    "have access to it."
)
_REUSABLE_ANSWER_STATUSES = {"grounded", "partially_grounded"}


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
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    conversation_id: uuid.UUID,
) -> list[MessageView]:
    await _owned_conversation(session, workspace_id, user_id, conversation_id)
    messages = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.role.desc())
    )
    return await _views(session, workspace_id, user_id, role, list(messages))


@dataclass(frozen=True)
class _Access:
    """What the reader may see now. Stored answers embed source content, so this is rechecked."""

    visible_sources: set[uuid.UUID]
    existing_documents: set[uuid.UUID]
    readable_tool_servers: set[uuid.UUID]

    def allows(self, citation: dict[str, Any]) -> bool:
        if citation.get("kind") == "tool":
            return uuid.UUID(citation["server_id"]) in self.readable_tool_servers
        return (
            uuid.UUID(citation["source_id"]) in self.visible_sources
            and uuid.UUID(citation["document_id"]) in self.existing_documents
        )


async def _views(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    messages: list[Message],
) -> list[MessageView]:
    citations = [citation for message in messages for citation in message.citations]
    cited_documents = {
        uuid.UUID(citation["document_id"])
        for citation in citations
        if citation.get("kind") != "tool"
    }
    existing_documents: set[uuid.UUID] = set()
    if cited_documents:
        existing_documents = set(
            await session.scalars(select(Document.id).where(Document.id.in_(cited_documents)))
        )
    readable_servers: set[uuid.UUID] = set()
    if has_permission(role, Permission.TOOLS_INVOKE) and any(
        citation.get("kind") == "tool" for citation in citations
    ):
        readable_servers = set(
            await session.scalars(
                select(ToolServer.id).where(
                    ToolServer.enabled.is_(True),
                    or_(ToolServer.owner_user_id.is_(None), ToolServer.owner_user_id == user_id),
                )
            )
        )
    access = _Access(
        await visible_source_ids(session, workspace_id, user_id),
        existing_documents,
        readable_servers,
    )
    return [_view(message, access) for message in messages]


def _revoked(citation: dict[str, Any]) -> dict[str, Any]:
    """Only identifiers remain: no title, excerpt or arguments."""
    if citation.get("kind") == "tool":
        return {
            "marker": citation["marker"],
            "kind": "tool",
            "used": citation.get("used", True),
            "access_revoked": True,
        }
    return {
        "marker": citation["marker"],
        "chunk_id": citation["chunk_id"],
        "document_id": citation["document_id"],
        "source_id": citation["source_id"],
        "used": citation.get("used", True),
        "access_revoked": True,
    }


def _view(message: Message, access: _Access) -> MessageView:
    citations = [
        citation if access.allows(citation) else _revoked(citation)
        for citation in message.citations
    ]
    withheld = any(citation.get("access_revoked") for citation in citations)
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
        "kind": "document",
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


def _tool_citation(index: int, item: ToolEvidence, *, used: bool) -> dict[str, Any]:
    """The gateway keeps no tool output. This excerpt lives in the asker's private conversation."""
    return {
        "marker": marker(index),
        "kind": "tool",
        "tool_id": item.tool_id,
        "server_id": str(item.server_id),
        "invocation_id": str(item.invocation_id),
        "fetched_at": item.fetched_at.isoformat(),
        "arguments": item.arguments,
        "excerpt": redact_secrets(item.content)[:_EXCERPT_SNAPSHOT_CHARS],
        "truncated": item.truncated,
        "used": used,
    }


def _all_citations(
    evidence: list[Evidence],
    tool_evidence: list[ToolEvidence],
    used_markers: frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Document markers come first, then tool results. `None` means every item counts as used."""

    def is_used(index: int) -> bool:
        return used_markers is None or marker(index) in used_markers

    documents = [_citation(i, item, used=is_used(i)) for i, item in enumerate(evidence)]
    offset = len(evidence)
    tools = [
        _tool_citation(offset + i, item, used=is_used(offset + i))
        for i, item in enumerate(tool_evidence)
    ]
    return [*documents, *tools]


def _excerpt_answer(evidence: list[Evidence], tool_evidence: list[ToolEvidence], lead: str) -> str:
    lines = [lead]
    for index, item in enumerate(evidence):
        snippet = " ".join(redact_secrets(item.content).split())[:300]
        lines.append(f"- {item.document_title}: {snippet} [{marker(index)}]")
    for offset, result in enumerate(tool_evidence):
        snippet = " ".join(redact_secrets(result.content).split())[:300]
        fetched = result.fetched_at.isoformat(timespec="seconds")
        index = len(evidence) + offset
        lines.append(f"- {result.tool_id}, fetched {fetched}: {snippet} [{marker(index)}]")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Generation:
    result: GenerationResult
    latency_ms: float


class _QuestionBudget:
    """Time and token limits for one question, across every model call it makes."""

    def __init__(self, *, max_seconds: float, max_tokens: int) -> None:
        self.deadline = time.monotonic() + max_seconds
        self._tokens_left = max_tokens

    def seconds_left(self) -> float:
        return self.deadline - time.monotonic()

    def tokens_left(self) -> int:
        return self._tokens_left

    def spend(self, prompt: str, result: GenerationResult) -> None:
        """Providers may omit counts. An estimate still has to count against the limit."""
        input_tokens = result.input_tokens or estimate_tokens(prompt)
        output_tokens = result.output_tokens or estimate_tokens(result.text)
        self._tokens_left -= input_tokens + output_tokens


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN_ESTIMATE


@dataclass
class _LiveContext:
    """What planning and tool calls produced for this question."""

    planner: str = "not_run"
    search_query: str | None = None
    records: list[ToolCallRecord] = field(default_factory=list)
    evidence: list[ToolEvidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    generations: list[_Generation] = field(default_factory=list)


def _plan_notes(plan: Plan, catalog: Catalog) -> list[str]:
    notes: list[str] = []
    if catalog.omitted:
        notes.append(f"{catalog.omitted} tool(s) did not fit in planning and were not considered.")
    if plan.unknown_tools:
        notes.append(f"{plan.unknown_tools} requested tool call(s) named unavailable tools.")
    if plan.malformed_calls:
        notes.append(f"{plan.malformed_calls} requested tool call(s) were malformed.")
    if plan.over_limit:
        notes.append(f"{plan.over_limit} requested tool call(s) exceeded the limit on calls.")
    return notes


def _tool_notes(records: list[ToolCallRecord]) -> list[str]:
    notes: list[str] = []
    for record in records:
        if record.status == InvocationStatus.SUCCEEDED:
            continue
        notes.append(
            f"{record.tool_id} did not return data ({record.code or record.status}), "
            "so its live state was not checked."
        )
    return notes


async def _plan_and_fetch(
    container: Container,
    actor: ToolActor,
    question: str,
    previous_questions: list[str],
    catalog: Catalog,
    budget: _QuestionBudget,
) -> _LiveContext:
    """Skipped entirely when there is nothing to plan. A failure here never fails the question."""
    llm = container.llm
    settings = container.settings
    if llm is None or not (catalog.tools or previous_questions):
        return _LiveContext()

    live = _LiveContext()
    prompt = build_planner_prompt(
        question,
        previous_questions,
        catalog,
        max_calls=settings.planner_max_tool_calls,
        now=utcnow(),
    )
    started = time.perf_counter()
    try:
        async with asyncio.timeout(min(settings.planner_timeout_seconds, budget.seconds_left())):
            generation = await llm.generate(
                system=PLANNER_SYSTEM_PROMPT,
                user=prompt,
                max_tokens=settings.planner_max_output_tokens,
                json_output=True,
            )
        latency = (time.perf_counter() - started) * 1000
        live.generations.append(_Generation(generation, latency))
        budget.spend(PLANNER_SYSTEM_PROMPT + prompt, generation)
        plan = parse_plan(
            generation.text,
            catalog,
            max_calls=settings.planner_max_tool_calls,
            max_query_chars=settings.max_question_chars,
        )
    except (ProviderUnavailable, PlanRejected, TimeoutError) as exc:
        log.warning("planning_failed", reason=str(exc) or type(exc).__name__)
        live.planner = "failed"
        live.notes.append(
            "Live tools were not consulted, because no plan could be made."
            if catalog.tools
            else "Earlier questions could not be used, so only this question was searched."
        )
        return live

    live.planner = "ran"
    # Without earlier questions there is nothing to resolve, so the user's own words are searched.
    live.search_query = plan.search_query if previous_questions else None
    live.notes.extend(_plan_notes(plan, catalog))
    live.records, live.evidence = await run_calls(
        container.tools,
        actor,
        plan.calls,
        deadline=budget.deadline,
        max_context_chars=settings.assistant_max_tool_context_chars,
    )
    live.notes.extend(_tool_notes(live.records))
    return live


@dataclass(frozen=True)
class _Outcome:
    content: str
    citations: list[dict[str, Any]]
    grounding: dict[str, Any]
    generation: _Generation | None


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
    container: Container,
    question: str,
    result: RetrievalResult,
    indexing: int,
    live: _LiveContext,
    history: list[HistoryTurn],
    budget: _QuestionBudget,
) -> _Outcome:
    llm = container.llm
    evidence = result.evidence
    tools = live.evidence
    grounding: dict[str, Any] = {
        "provider": llm.name if llm else None,
        "model": llm.model if llm else None,
        "dense_available": result.dense_available,
        "uncited_sentences": 0,
        "removed_citations": [],
        "stale_sources": sum(1 for item in evidence if item.stale),
        "notes": list(live.notes),
        "planner": live.planner,
        "search_query": live.search_query,
        "tool_calls": [record.as_json() for record in live.records],
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
    if any(item.truncated for item in tools):
        grounding["notes"].append(
            "At least one live result was cut off, so it may not show everything."
        )

    if not evidence and not tools:
        # Never ask a model to answer with nothing to ground it.
        return _Outcome(_NO_EVIDENCE_TEXT, [], {**grounding, "status": "no_evidence"}, None)

    all_cited = _all_citations(evidence, tools)
    if llm is None:
        text = _excerpt_answer(evidence, tools, "No model is configured. Most relevant excerpts:")
        return _Outcome(text, all_cited, {**grounding, "status": "extractive"}, None)

    unavailable = [
        record.tool_id for record in live.records if record.status != InvocationStatus.SUCCEEDED
    ]
    prompt = build_user_prompt(question, evidence, tools, history, unavailable)
    return await _generate_answer(
        container, llm, prompt, evidence, tools, grounding, all_cited, budget
    )


def _over_budget(
    evidence: list[Evidence],
    tools: list[ToolEvidence],
    grounding: dict[str, Any],
    all_cited: list[dict[str, Any]],
) -> _Outcome:
    text = _excerpt_answer(
        evidence, tools, "The limit for this question was reached, so no answer was generated:"
    )
    return _Outcome(text, all_cited, {**grounding, "status": "budget_exceeded"}, None)


async def _generate_answer(
    container: Container,
    llm: LLMProvider,
    prompt: str,
    evidence: list[Evidence],
    tools: list[ToolEvidence],
    grounding: dict[str, Any],
    all_cited: list[dict[str, Any]],
    budget: _QuestionBudget,
) -> _Outcome:
    tokens = budget.tokens_left() - estimate_tokens(SYSTEM_PROMPT + prompt)
    if tokens < _MIN_ANSWER_TOKENS or budget.seconds_left() <= 0:
        return _over_budget(evidence, tools, grounding, all_cited)

    started = time.perf_counter()
    try:
        async with asyncio.timeout(budget.seconds_left()):
            generation = await llm.generate(
                system=SYSTEM_PROMPT,
                user=prompt,
                max_tokens=min(container.settings.llm_max_output_tokens, tokens),
            )
    except TimeoutError:
        return _over_budget(evidence, tools, grounding, all_cited)
    except ProviderUnavailable as exc:
        log.warning("model_unavailable", provider=llm.name, reason=str(exc))
        text = _excerpt_answer(
            evidence,
            tools,
            "The model is unavailable, so no answer was generated. Relevant excerpts:",
        )
        return _Outcome(text, all_cited, {**grounding, "status": "model_unavailable"}, None)
    latency = (time.perf_counter() - started) * 1000

    valid = {marker(i) for i in range(len(evidence) + len(tools))}
    validated = validate_answer(generation.text, valid)
    return _finalize(validated, evidence, tools, grounding, _Generation(generation, latency))


def _finalize(
    validated: ValidatedAnswer,
    evidence: list[Evidence],
    tools: list[ToolEvidence],
    grounding: dict[str, Any],
    generation: _Generation,
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
    citations = _all_citations(evidence, tools, validated.used_markers)
    if validated.says_not_found:
        grounding["status"] = "no_evidence"
        if tools:
            # "Nothing found" must not read as "the system holds nothing": the model decided that.
            grounding["notes"] = [
                *grounding["notes"],
                "A live system returned data, but the model did not find the answer in it.",
            ]
        return _Outcome(NOT_FOUND_SENTINEL, [], grounding, generation)
    if not validated.used_markers:
        # An uncited generated answer is not evidence-backed. Show the excerpts, not the claims.
        grounding["status"] = "unsupported"
        grounding["notes"] = [
            *grounding["notes"],
            "The model's answer cited no sources, so it was discarded.",
        ]
        text = _excerpt_answer(
            evidence, tools, "A verifiable answer could not be generated. Most relevant excerpts:"
        )
        return _Outcome(text, _all_citations(evidence, tools), grounding, generation)
    grounding["status"] = "partially_grounded" if validated.uncited_sentences else "grounded"
    if validated.uncited_sentences:
        grounding["notes"] = [
            *grounding["notes"],
            f"{validated.uncited_sentences} sentence(s) have no citation. Verify them.",
        ]
    return _Outcome(validated.text, citations, grounding, generation)


async def _recent_history(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
    conversation_id: uuid.UUID,
    *,
    turns: int,
) -> list[HistoryTurn]:
    """Earlier exchanges, oldest first. An answer is dropped unless it can still be read in full."""
    newest_first = await session.scalars(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.role)
        .limit(turns * 2)
    )
    views = await _views(session, workspace_id, user_id, role, list(newest_first)[::-1])
    history: list[HistoryTurn] = []
    for view in views:
        if view.message.role == MessageRole.USER:
            history.append(HistoryTurn(view.content, None))
            continue
        status = (view.grounding or {}).get("status")
        if history and history[-1].answer is None and status in _REUSABLE_ANSWER_STATUSES:
            history[-1] = HistoryTurn(history[-1].question, view.content)
    return history


async def _search(
    container: Container, workspace_id: uuid.UUID, user_id: uuid.UUID, query: str
) -> RetrievalResult:
    # Embed before opening a transaction. On failure, fall back to lexical search and say so.
    query_embedding: list[float] | None = None
    try:
        query_embedding = await container.embedder.embed_query(query)
    except ProviderUnavailable as exc:
        log.warning("embedding_unavailable", reason=str(exc))

    async with tenant_scope(container.session_factory, workspace_id) as session:
        return await retrieve(
            session,
            settings=container.settings,
            workspace_id=workspace_id,
            user_id=user_id,
            query=query,
            query_embedding=query_embedding,
            embedding_model=container.embedder.model,
        )


async def ask(
    container: Container,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: Role,
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
    budget = _QuestionBudget(
        max_seconds=settings.assistant_max_seconds,
        max_tokens=settings.assistant_max_tokens_per_question,
    )

    async with tenant_scope(factory, workspace_id) as session:
        conversation = await _owned_conversation(session, workspace_id, user_id, conversation_id)
        history = await _recent_history(
            session,
            workspace_id,
            user_id,
            role,
            conversation_id,
            turns=settings.assistant_history_turns,
        )
        catalog = Catalog()
        if has_permission(role, Permission.TOOLS_INVOKE):
            catalog = await load_catalog(
                session,
                user_id,
                max_chars=settings.planner_max_catalog_chars,
                allow_mail=not settings.uses_external_model
                or settings.external_model_mail_approved,
            )
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

    # No transaction is open during model or tool calls, so they do not hold a connection.
    live = await _plan_and_fetch(
        container,
        ToolActor(workspace_id, user_id, role),
        question,
        [turn.question for turn in history],
        catalog,
        budget,
    )

    # The user's own words come first. Models can garble a rewrite, as the planner evaluation
    # showed, so it is tried only when those words find nothing.
    result = await _search(container, workspace_id, user_id, question)
    rewrite = live.search_query
    if not result.evidence and rewrite and rewrite != question:
        result = await _search(container, workspace_id, user_id, rewrite)
    else:
        live.search_query = None

    async with tenant_scope(factory, workspace_id) as session:
        indexing = await _documents_being_indexed(session, workspace_id, user_id)

    outcome = await _answer(container, question, result, indexing, live, history, budget)

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
        generations = [*live.generations]
        if outcome.generation is not None:
            generations.append(outcome.generation)
        if container.llm is not None:
            for generation in generations:
                session.add(
                    UsageRecord(
                        workspace_id=workspace_id,
                        user_id=user_id,
                        message_id=answer.id,
                        provider=container.llm.name,
                        model=container.llm.model,
                        input_tokens=generation.result.input_tokens,
                        output_tokens=generation.result.output_tokens,
                        latency_ms=generation.latency_ms,
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
                "planner": live.planner,
                "tool_ids": sorted({record.tool_id for record in live.records}),
            },
        )
        # Reload the question: its row belongs to the earlier, closed session.
        stored_question = await session.get(Message, user_message.id)
        assert stored_question is not None  # noqa: S101 - committed above in this request
        views = await _views(session, workspace_id, user_id, role, [stored_question, answer])
    return views[0], views[1]
