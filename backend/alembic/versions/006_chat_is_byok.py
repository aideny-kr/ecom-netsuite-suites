"""Add is_byok flag to chat_messages

Revision ID: 006_chat_is_byok
Revises: 005_rename_plans
Create Date: 2026-02-16
"""

import sqlalchemy as sa

from alembic import op

revision = "006_chat_is_byok"
down_revision = "005_rename_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("is_byok", sa.Boolean(), nullable=True, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "is_byok")
