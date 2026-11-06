"""Connections owned by one person, Gmail as a tool server kind, and OAuth sign-in state.

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "opsassist_app"

_TARGET_WITH_GMAIL = (
    "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL AND connection_id IS NULL) OR "
    "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL AND connection_id IS NULL) OR "
    "(kind = 'github' AND connection_id IS NOT NULL AND url IS NULL AND stdio_name IS NULL) OR "
    "(kind = 'gmail' AND connection_id IS NOT NULL AND url IS NULL AND stdio_name IS NULL "
    "AND owner_user_id IS NOT NULL)"
)
_TARGET_WITHOUT_GMAIL = (
    "(kind = 'mcp_http' AND url IS NOT NULL AND stdio_name IS NULL AND connection_id IS NULL) OR "
    "(kind = 'mcp_stdio' AND stdio_name IS NOT NULL AND url IS NULL AND connection_id IS NULL) OR "
    "(kind = 'github' AND connection_id IS NOT NULL AND url IS NULL AND stdio_name IS NULL)"
)


def upgrade() -> None:
    # CASCADE, never SET NULL: a null owner means shared, so losing one must not share a mailbox.
    for table in ("integration_connections", "tool_servers"):
        op.add_column(
            table,
            sa.Column(
                "owner_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
            ),
        )
    op.create_index(
        "uq_connections_owner_provider",
        "integration_connections",
        ["workspace_id", "owner_user_id", "provider"],
        unique=True,
        postgresql_where=sa.text("owner_user_id IS NOT NULL"),
    )
    op.drop_constraint("target_matches_kind", "tool_servers", type_="check")
    op.create_check_constraint("target_matches_kind", "tool_servers", _TARGET_WITH_GMAIL)

    # No RLS: the sign-in callback finds the workspace from this row.
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(64), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_oauth_states_expires", "oauth_states", ["expires_at"])
    op.execute(f"GRANT SELECT, INSERT, DELETE ON oauth_states TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("oauth_states")
    op.execute("DELETE FROM tool_servers WHERE kind = 'gmail'")
    op.execute("DELETE FROM integration_connections WHERE provider = 'gmail'")
    op.drop_constraint("target_matches_kind", "tool_servers", type_="check")
    op.create_check_constraint("target_matches_kind", "tool_servers", _TARGET_WITHOUT_GMAIL)
    op.drop_index("uq_connections_owner_provider", table_name="integration_connections")
    op.drop_column("tool_servers", "owner_user_id")
    op.drop_column("integration_connections", "owner_user_id")
