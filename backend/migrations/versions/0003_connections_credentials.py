"""Integration connections, encrypted credentials, and GitHub as a tool server kind.

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "opsassist_app"
TABLES = ("integration_connections", "stored_credentials")
CURRENT_WORKSPACE = "NULLIF(current_setting('app.workspace_id', true), '')::uuid"

_TARGET_WITH_GITHUB = (
    "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL AND connection_id IS NULL) OR "
    "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL AND connection_id IS NULL) OR "
    "(kind = 'github' AND connection_id IS NOT NULL AND url IS NULL AND stdio_name IS NULL)"
)
_TARGET_WITHOUT_GITHUB = (
    "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL) OR "
    "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL)"
)


def upgrade() -> None:
    op.create_table(
        "stored_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=False),
        sa.Column("wrapped_key_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "integration_connections",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("credential_id", UUID(as_uuid=True)),
        sa.Column("last_verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "name"),
    )
    op.add_column(
        "tool_servers",
        sa.Column(
            "connection_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integration_connections.id", ondelete="CASCADE"),
        ),
    )
    op.drop_constraint("target_matches_kind", "tool_servers", type_="check")
    op.create_check_constraint("target_matches_kind", "tool_servers", _TARGET_WITH_GITHUB)

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} "
            f"USING (workspace_id = {CURRENT_WORKSPACE}) "
            f"WITH CHECK (workspace_id = {CURRENT_WORKSPACE})"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {APP_ROLE}")


def downgrade() -> None:
    op.execute("DELETE FROM tool_servers WHERE kind = 'github'")
    op.drop_constraint("target_matches_kind", "tool_servers", type_="check")
    op.create_check_constraint("target_matches_kind", "tool_servers", _TARGET_WITHOUT_GITHUB)
    op.drop_column("tool_servers", "connection_id")
    op.drop_table("integration_connections")
    op.drop_table("stored_credentials")
