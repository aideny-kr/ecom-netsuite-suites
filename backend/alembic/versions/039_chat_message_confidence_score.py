"""Add confidence_score to chat_messages."""

import sqlalchemy as sa

from alembic import op

revision = "039_confidence_score"
down_revision = "038_chat_message_content_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("confidence_score", sa.Numeric(precision=3, scale=1), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "confidence_score")
