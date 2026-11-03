"""Tool layer: servers, reviewed definitions and the invocation log.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "opsassist_app"
TABLES = ("tool_servers", "tool_definitions", "tool_invocations")
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
    op.create_table(
        "tool_servers",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("url", sa.String(2000)),
        sa.Column("stdio_name", sa.String(100)),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health", sa.String(20), nullable=False),
        sa.Column("last_error_code", sa.String(50)),
        _ts("last_checked_at", nullable=True),
        _ts("last_discovered_at", nullable=True),
        _fk("created_by", "users.id", nullable=True, ondelete="SET NULL"),
        _ts("created_at"),
        sa.UniqueConstraint("workspace_id", "slug"),
        sa.CheckConstraint(
            "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL) OR "
            "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL)",
            name="target_matches_kind",
        ),
    )
    op.create_table(
        "tool_definitions",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        _fk("server_id", "tool_servers.id"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("tool_id", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("input_schema", JSONB(), nullable=False),
        sa.Column("annotations", JSONB(), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False),
        sa.Column("risk_level", sa.Integer(), nullable=False),
        sa.Column("access", sa.String(10), nullable=False),
        sa.Column("required_scopes", ARRAY(sa.Text()), nullable=False),
        sa.Column("timeout_seconds", sa.Float()),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("pending_definition", JSONB()),
        sa.Column("pending_hash", sa.String(64)),
        _fk("reviewed_by", "users.id", nullable=True, ondelete="SET NULL"),
        _ts("reviewed_at", nullable=True),
        _ts("first_seen_at"),
        _ts("last_seen_at"),
        sa.UniqueConstraint("server_id", "name"),
        sa.UniqueConstraint("workspace_id", "tool_id"),
        sa.CheckConstraint("risk_level BETWEEN 0 AND 3", name="risk_level_valid"),
        sa.CheckConstraint("access IN ('read', 'write')", name="access_valid"),
    )
    op.create_table(
        "tool_invocations",
        _id(),
        _fk("workspace_id", "workspaces.id"),
        sa.Column("actor_user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tool_definition_id", UUID(as_uuid=True), nullable=False),
        sa.Column("server_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tool_id", sa.String(200), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float()),
        sa.Column("output_chars", sa.Integer()),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
    )
    op.create_index(
        "ix_tool_invocations_workspace_time", "tool_invocations", ["workspace_id", "created_at"]
    )
    op.create_index(
        "ix_tool_invocations_tool", "tool_invocations", ["tool_definition_id", "created_at"]
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (workspace_id = {CURRENT_WORKSPACE}) "
            f"WITH CHECK (workspace_id = {CURRENT_WORKSPACE})"
        )

    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tool_servers TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON tool_definitions TO {APP_ROLE}")
    # No DELETE: the invocation history is a record. UPDATE is needed to close a running call.
    op.execute(f"GRANT SELECT, INSERT, UPDATE ON tool_invocations TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("tool_invocations")
    op.drop_table("tool_definitions")
    op.drop_table("tool_servers")
