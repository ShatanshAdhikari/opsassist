"""Initial schema: tenancy, identity, audit, jobs, knowledge, assistant, RLS and grants.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "opsassist_app"

TENANT_TABLES = (
    "groups",
    "group_members",
    "knowledge_sources",
    "source_grants",
    "documents",
    "document_versions",
    "chunks",
    "conversations",
    "messages",
    "usage_records",
)

CURRENT_WORKSPACE = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"


def _id() -> sa.Column:
    return sa.Column("id", UUID(as_uuid=True), primary_key=True)


def _ts(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _fk(name: str, target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> sa.Column:
    return sa.Column(
        name, UUID(as_uuid=True), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "organizations",
        _id(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        _ts("created_at"),
    )
    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        _ts("created_at"),
    )
    op.create_index("uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True)
    op.create_table(
        "workspaces",
        _id(),
        _fk("organization_id", "organizations.id"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("organization_id", "slug"),
    )
    op.create_table(
        "memberships",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("user_id", "users.id"),
        sa.Column("role", sa.String(20), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "user_id"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table(
        "sessions",
        _id(),
        _fk("user_id", "users.id"),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        _ts("created_at"),
        _ts("last_seen_at"),
        _ts("expires_at"),
        _ts("revoked_at", nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_table(
        "audit_events",
        _id(),
        _ts("occurred_at"),
        sa.Column("workspace_id", UUID(as_uuid=True)),
        sa.Column("actor_user_id", UUID(as_uuid=True)),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(50)),
        sa.Column("target_id", sa.String(200)),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("request_id", sa.String(64)),
        sa.Column("metadata", JSONB(), nullable=False),
    )
    op.create_index(
        "ix_audit_events_workspace_time", "audit_events", ["workspace_id", "occurred_at"]
    )
    op.create_index("ix_audit_events_actor_time", "audit_events", ["actor_user_id", "occurred_at"])
    op.create_table(
        "jobs",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        _ts("run_after"),
        sa.Column("locked_by", sa.String(100)),
        _ts("lease_expires_at", nullable=True),
        sa.Column("last_error", sa.Text()),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
    )
    op.create_index(
        "ix_jobs_claimable", "jobs", ["run_after"], postgresql_where=sa.text("status = 'queued'")
    )
    op.create_index(
        "ix_jobs_running_lease",
        "jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "groups",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("name", sa.String(200), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "name"),
    )
    op.create_table(
        "group_members",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("group_id", "groups.id"),
        _fk("user_id", "users.id"),
        sa.UniqueConstraint("group_id", "user_id"),
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])
    op.create_table(
        "knowledge_sources",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("visibility", sa.String(20), nullable=False),
        _fk("created_by", "users.id", nullable=True, ondelete="SET NULL"),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "name"),
        sa.CheckConstraint("visibility IN ('workspace', 'restricted')", name="visibility_valid"),
    )
    op.create_table(
        "source_grants",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("source_id", "knowledge_sources.id"),
        _fk("user_id", "users.id", nullable=True),
        _fk("group_id", "groups.id", nullable=True),
        _fk("created_by", "users.id", nullable=True, ondelete="SET NULL"),
        _ts("created_at"),
        sa.CheckConstraint("(user_id IS NULL) <> (group_id IS NULL)", name="exactly_one_grantee"),
    )
    op.create_index(
        "uq_source_grants_user",
        "source_grants",
        ["source_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
    )
    op.create_index(
        "uq_source_grants_group",
        "source_grants",
        ["source_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_table(
        "documents",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("source_id", "knowledge_sources.id"),
        sa.Column("external_id", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("current_version_id", UUID(as_uuid=True)),
        _fk("created_by", "users.id", nullable=True, ondelete="SET NULL"),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("source_id", "external_id"),
    )
    op.create_index("ix_documents_source_id", "documents", ["source_id"])
    op.create_table(
        "document_versions",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("document_id", "documents.id"),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("blob_key", sa.String(200), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("embedding_model", sa.String(100)),
        _ts("created_at"),
        _ts("indexed_at", nullable=True),
        sa.UniqueConstraint("document_id", "version_number"),
    )
    op.create_table(
        "chunks",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("source_id", "knowledge_sources.id"),
        _fk("document_id", "documents.id"),
        _fk("version_id", "document_versions.id"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("section_path", ARRAY(sa.Text()), nullable=False),
        sa.Column("page", sa.Integer()),
        sa.Column("line_start", sa.Integer()),
        sa.Column("line_end", sa.Integer()),
        sa.Column("embedding", Vector(768), nullable=False),
        sa.Column("embedding_model", sa.String(100), nullable=False),
        sa.Column("lexical_text", sa.Text(), nullable=False),
        sa.Column(
            "tsv", TSVECTOR(), sa.Computed("to_tsvector('english', lexical_text)", persisted=True)
        ),
        sa.UniqueConstraint("version_id", "ordinal"),
    )
    op.create_index("ix_chunks_source_id", "chunks", ["source_id"])
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_table(
        "conversations",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("user_id", "users.id"),
        sa.Column("title", sa.String(200), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index(
        "ix_conversations_user", "conversations", ["workspace_id", "user_id", "updated_at"]
    )
    op.create_table(
        "messages",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("conversation_id", "conversations.id"),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", JSONB(), nullable=False),
        sa.Column("grounding", JSONB(), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_messages_conversation", "messages", ["conversation_id", "created_at"])
    op.create_table(
        "usage_records",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("user_id", "users.id", nullable=True, ondelete="SET NULL"),
        _fk("message_id", "messages.id", nullable=True, ondelete="SET NULL"),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_usage_workspace_time", "usage_records", ["workspace_id", "created_at"])

    # Row-level security. FORCE also subjects a non-superuser table owner to the policy.
    # With app.workspace_id unset the expression is NULL, so no row matches: fail closed.
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (workspace_id = {CURRENT_WORKSPACE}) "
            f"WITH CHECK (workspace_id = {CURRENT_WORKSPACE})"
        )

    # Audit: read within the workspace; insert in the workspace or with no workspace
    # (authentication events). No UPDATE or DELETE grant exists for the application.
    op.execute("ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE audit_events FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY audit_read ON audit_events FOR SELECT "
        f"USING (workspace_id = {CURRENT_WORKSPACE})"
    )
    op.execute(
        f"CREATE POLICY audit_insert ON audit_events FOR INSERT "
        f"WITH CHECK (workspace_id IS NULL OR workspace_id = {CURRENT_WORKSPACE})"
    )

    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON organizations, workspaces, users TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON memberships, sessions TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON jobs TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_events TO {APP_ROLE}")
    tables = ", ".join(TENANT_TABLES)
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO {APP_ROLE}")


def downgrade() -> None:
    for table in (
        "usage_records",
        "messages",
        "conversations",
        "chunks",
        "document_versions",
        "documents",
        "source_grants",
        "knowledge_sources",
        "group_members",
        "groups",
        "jobs",
        "audit_events",
        "sessions",
        "memberships",
        "workspaces",
        "users",
        "organizations",
    ):
        op.drop_table(table)
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
