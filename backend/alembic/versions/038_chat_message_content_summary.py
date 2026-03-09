"""Add content_summary column to chat_messages.

Revision ID: 038
Revises: 037
Create Date: 2026-03-09
"""

import sqlalchemy as sa

from alembic import op

revision = "038_chat_message_content_summary"
down_revision = "037_tenant_feature_flags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("content_summary", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "content_summary")
