"""Add workspace_id to chat_sessions for workspace-scoped chat.

Revision ID: 013_chat_workspace_scope
Revises: 012_onboarding_policy_fields
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "013_chat_workspace_scope"
down_revision = "012_onboarding_policy_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_chat_sessions_workspace_id",
        "chat_sessions",
        ["workspace_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_workspace_id", table_name="chat_sessions")
    op.drop_column("chat_sessions", "workspace_id")
