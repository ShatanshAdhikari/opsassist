"""Request and response models. Request bodies reject unknown fields."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opsassist.models import Role, SourceVisibility


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseBody(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- Identity ---------------------------------------------------------------------------


class LoginRequest(RequestBody):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserOut(ResponseBody):
    id: uuid.UUID
    email: str
    display_name: str


class WorkspaceOut(ResponseBody):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    slug: str
    role: Role


class MeOut(BaseModel):
    user: UserOut
    workspaces: list[WorkspaceOut]


class AddMemberRequest(RequestBody):
    email: str = Field(min_length=3, max_length=320)
    role: Role = Role.MEMBER


class MemberOut(BaseModel):
    user: UserOut
    role: Role


class CreateGroupRequest(RequestBody):
    name: str = Field(min_length=1, max_length=200)


class GroupOut(ResponseBody):
    id: uuid.UUID
    name: str


class AddGroupMemberRequest(RequestBody):
    user_id: uuid.UUID


class AuditEventOut(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    request_id: str | None
    metadata: dict[str, Any]


# --- Knowledge --------------------------------------------------------------------------


class CreateSourceRequest(RequestBody):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    visibility: SourceVisibility = SourceVisibility.WORKSPACE


class SourceOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    kind: str
    visibility: SourceVisibility
    created_at: datetime
    document_count: int
    can_read: bool


class CreateGrantRequest(RequestBody):
    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None


class GrantOut(ResponseBody):
    id: uuid.UUID
    user_id: uuid.UUID | None
    group_id: uuid.UUID | None


class DocumentOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    title: str
    content_type: str
    updated_at: datetime
    current_version_number: int | None
    latest_version_number: int
    latest_status: str
    latest_error: str | None
    chunk_count: int
    indexed_at: datetime | None


class UploadOut(BaseModel):
    document: DocumentOut
    deduplicated: bool


class ChunkOut(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_name: str
    version_number: int
    content: str
    section_path: list[str]
    page: int | None
    line_start: int | None
    line_end: int | None


class SearchRequest(RequestBody):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=8, ge=1, le=20)


class SearchHit(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    document_title: str
    source_name: str
    version_number: int
    excerpt: str
    section_path: list[str]
    page: int | None
    line_start: int | None
    line_end: int | None
    score: float
    matched_by: list[str]
    indexed_at: datetime
    stale: bool


class SearchOut(BaseModel):
    hits: list[SearchHit]
    dense_available: bool


# --- Assistant --------------------------------------------------------------------------


class CreateConversationRequest(RequestBody):
    title: str = Field(default="New conversation", min_length=1, max_length=200)


class ConversationOut(ResponseBody):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class AskRequest(RequestBody):
    question: str = Field(min_length=1, max_length=4000)


class CitationOut(BaseModel):
    marker: str
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source_id: uuid.UUID
    document_title: str | None = None
    source_name: str | None = None
    version_number: int | None = None
    excerpt: str | None = None
    section_path: list[str] = []
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    indexed_at: str | None = None
    stale: bool = False
    used: bool = True
    access_revoked: bool = False


GroundingStatus = Literal[
    "grounded",
    "partially_grounded",
    "unsupported",
    "extractive",
    "no_evidence",
    "model_unavailable",
    "withheld",
]


class GroundingOut(BaseModel):
    status: GroundingStatus
    provider: str | None = None
    model: str | None = None
    dense_available: bool = True
    uncited_sentences: int = 0
    removed_citations: list[str] = []
    stale_sources: int = 0
    notes: list[str] = []


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[CitationOut]
    grounding: GroundingOut | None
    created_at: datetime


class AskOut(BaseModel):
    user_message: MessageOut
    answer: MessageOut
