"""Conversations and cited questions."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from opsassist.api.deps import WorkspaceDep
from opsassist.api.schemas import (
    AskOut,
    AskRequest,
    CitationOut,
    ConversationOut,
    CreateConversationRequest,
    GroundingOut,
    MessageOut,
)
from opsassist.assistant import service
from opsassist.identity.permissions import Permission

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["assistant"])


def _message_out(view: service.MessageView) -> MessageOut:
    return MessageOut(
        id=view.message.id,
        role=view.message.role,
        content=view.content,
        citations=[CitationOut(**citation) for citation in view.citations],
        grounding=GroundingOut(**view.grounding) if view.grounding else None,
        created_at=view.message.created_at,
    )


@router.get("/conversations")
async def list_conversations(ctx: WorkspaceDep) -> list[ConversationOut]:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    async with ctx.tx() as session:
        conversations = await service.list_conversations(session, ctx.workspace_id, ctx.user.id)
    return [ConversationOut.model_validate(item) for item in conversations]


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest, ctx: WorkspaceDep
) -> ConversationOut:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    async with ctx.tx() as session:
        conversation = await service.create_conversation(
            session, ctx.workspace_id, ctx.user.id, body.title
        )
    return ConversationOut.model_validate(conversation)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(conversation_id: uuid.UUID, ctx: WorkspaceDep) -> list[MessageOut]:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    async with ctx.tx() as session:
        views = await service.list_messages(session, ctx.workspace_id, ctx.user.id, conversation_id)
    return [_message_out(view) for view in views]


@router.post("/conversations/{conversation_id}/messages")
async def ask(conversation_id: uuid.UUID, body: AskRequest, ctx: WorkspaceDep) -> AskOut:
    ctx.require(Permission.KNOWLEDGE_QUERY)
    question_view, answer_view = await service.ask(
        ctx.container,
        workspace_id=ctx.workspace_id,
        user_id=ctx.user.id,
        conversation_id=conversation_id,
        question=body.question,
    )
    return AskOut(user_message=_message_out(question_view), answer=_message_out(answer_view))
