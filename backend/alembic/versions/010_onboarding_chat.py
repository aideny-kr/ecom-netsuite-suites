"""Add session_type column to chat_sessions for onboarding chat

Revision ID: 010_onboarding_chat
Revises: 009_workspace_runs
Create Date: 2026-02-17
"""

import sqlalchemy as sa

from alembic import op

revision = "010_onboarding_chat"
down_revision = "009_workspace_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("session_type", sa.String(20), nullable=False, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "session_type")
