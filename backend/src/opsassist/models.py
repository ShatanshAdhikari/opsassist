"""SQLAlchemy models. Schema changes go through Alembic migrations, never create_all."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBEDDING_DIMENSIONS = 768


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class SourceVisibility(StrEnum):
    WORKSPACE = "workspace"
    RESTRICTED = "restricted"


class VersionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


# --- Tenancy and identity (no RLS: reached before a workspace context exists) -----------


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("uq_users_email_lower", text("lower(email)"), unique=True),)

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    id: Mapped[uuid.UUID] = _pk()
    organization_id: Mapped[uuid.UUID] = _fk("organizations.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        Index("ix_memberships_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """Append-only: the application role holds INSERT and SELECT only."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_workspace_time", "workspace_id", "occurred_at"),
        Index("ix_audit_events_actor_time", "actor_user_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    occurred_at: Mapped[datetime] = _created_at()
    # Plain references, not foreign keys: audit outlives the rows it describes.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="success")
    request_id: Mapped[str | None] = mapped_column(String(64))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )


class Job(Base):
    """Durable queue. Not RLS-protected: workers claim across workspaces.

    Payloads hold identifiers only, never content.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_claimable",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index(
            "ix_jobs_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    run_after: Mapped[datetime] = _created_at()
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint("group_id", "user_id"),
        Index("ix_group_members_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    group_id: Mapped[uuid.UUID] = _fk("groups.id")
    user_id: Mapped[uuid.UUID] = _fk("users.id")


# --- Knowledge (RLS by workspace) -------------------------------------------------------


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        CheckConstraint("visibility IN ('workspace', 'restricted')", name="visibility_valid"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(30), nullable=False, default="upload")
    visibility: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created_at()


class SourceGrant(Base):
    __tablename__ = "source_grants"
    __table_args__ = (
        CheckConstraint("(user_id IS NULL) <> (group_id IS NULL)", name="exactly_one_grantee"),
        Index(
            "uq_source_grants_user",
            "source_id",
            "user_id",
            unique=True,
            postgresql_where=text("user_id IS NOT NULL"),
        ),
        Index(
            "uq_source_grants_group",
            "source_id",
            "group_id",
            unique=True,
            postgresql_where=text("group_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    source_id: Mapped[uuid.UUID] = _fk("knowledge_sources.id")
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True)
    group_id: Mapped[uuid.UUID | None] = _fk("groups.id", nullable=True)
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        Index("ix_documents_source_id", "source_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    source_id: Mapped[uuid.UUID] = _fk("knowledge_sources.id")
    external_id: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # Nullable until the first version is indexed. Retrieval joins on this, so a version
    # becomes searchable atomically when it is activated.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_number"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    document_id: Mapped[uuid.UUID] = _fk("documents.id")
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_key: Mapped[str] = mapped_column(String(200), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=VersionStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = _created_at()
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("version_id", "ordinal"),
        Index("ix_chunks_source_id", "source_id"),
        Index("ix_chunks_document_id", "document_id"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    source_id: Mapped[uuid.UUID] = _fk("knowledge_sources.id")
    document_id: Mapped[uuid.UUID] = _fk("documents.id")
    version_id: Mapped[uuid.UUID] = _fk("document_versions.id")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    section_path: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    page: Mapped[int | None] = mapped_column(Integer)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(100), nullable=False)
    lexical_text: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', lexical_text)", persisted=True)
    )


class OAuthState(Base):
    """One sign-in in progress. No RLS: the callback finds the workspace from this row.

    Only a hash of the state is stored. The verifier is single-use, expires in minutes, and is
    useless without the authorization code.
    """

    __tablename__ = "oauth_states"
    __table_args__ = (Index("ix_oauth_states_expires", "expires_at"),)

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --- Connections and credentials (RLS by workspace) --------------------------------------


class StoredCredential(Base):
    """Encrypted secret material. No API response model reads this table.

    Only ciphertext is here: decryption also needs a key held in deployment configuration.
    """

    __tablename__ = "stored_credentials"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    wrapped_key_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IntegrationConnection(Base):
    """A link to an external system. `config` holds settings, never secrets.

    With `owner_user_id` set it is one person's, such as a mailbox, and nobody else may use it.
    Without, it is shared across the workspace.
    """

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "name"),
        Index(
            "uq_connections_owner_provider",
            "workspace_id",
            "owner_user_id",
            "provider",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    owner_user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created_at()


# --- Tools (RLS by workspace) -----------------------------------------------------------


class ToolServer(Base):
    __tablename__ = "tool_servers"
    __table_args__ = (
        UniqueConstraint("workspace_id", "slug"),
        CheckConstraint(
            "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL "
            "AND connection_id IS NULL) OR "
            "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL "
            "AND connection_id IS NULL) OR "
            "(kind = 'github' AND connection_id IS NOT NULL AND url IS NULL "
            "AND stdio_name IS NULL) OR "
            "(kind = 'gmail' AND connection_id IS NOT NULL AND url IS NULL "
            "AND stdio_name IS NULL AND owner_user_id IS NOT NULL)",
            name="target_matches_kind",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    owner_user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2000))
    stdio_name: Mapped[str | None] = mapped_column(String(100))
    connection_id: Mapped[uuid.UUID | None] = _fk("integration_connections.id", nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    health: Mapped[str] = mapped_column(String(20), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(50))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    created_at: Mapped[datetime] = _created_at()


class ToolDefinition(Base):
    """The reviewed, pinned description of a tool.

    `description`, `input_schema` and `annotations` are what the admin approved. A newer version
    from the provider waits in `pending_definition` and has no effect until reviewed.
    """

    __tablename__ = "tool_definitions"
    __table_args__ = (
        UniqueConstraint("server_id", "name"),
        UniqueConstraint("workspace_id", "tool_id"),
        CheckConstraint("risk_level BETWEEN 0 AND 3", name="risk_level_valid"),
        CheckConstraint("access IN ('read', 'write')", name="access_valid"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    server_id: Mapped[uuid.UUID] = _fk("tool_servers.id")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    annotations: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), nullable=False)
    risk_level: Mapped[int] = mapped_column(Integer, nullable=False)
    access: Mapped[str] = mapped_column(String(10), nullable=False)
    required_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    timeout_seconds: Mapped[float | None] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    pending_definition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pending_hash: Mapped[str | None] = mapped_column(String(64))
    reviewed_by: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _created_at()
    last_seen_at: Mapped[datetime] = _created_at()


class ToolInvocation(Base):
    """One attempted call. Holds a hash of the arguments, never the arguments or the output.

    Tool and server are plain references, not foreign keys, so history outlives a deleted server.
    The application role cannot delete these rows.
    """

    __tablename__ = "tool_invocations"
    __table_args__ = (
        Index("ix_tool_invocations_workspace_time", "workspace_id", "created_at"),
        Index("ix_tool_invocations_tool", "tool_definition_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    actor_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool_definition_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    server_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tool_id: Mapped[str] = mapped_column(String(200), nullable=False)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(50))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    output_chars: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --- Assistant (RLS by workspace) -------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user", "workspace_id", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _created_at()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation", "conversation_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    conversation_id: Mapped[uuid.UUID] = _fk("conversations.id")
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    grounding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()


class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_workspace_time", "workspace_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _fk("workspaces.id")
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    message_id: Mapped[uuid.UUID | None] = _fk("messages.id", nullable=True, ondelete="SET NULL")
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = _created_at()
