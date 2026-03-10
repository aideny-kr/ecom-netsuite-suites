"""Add query_importance to chat_messages."""

import sqlalchemy as sa

from alembic import op

revision = "040_query_importance"
down_revision = "039_confidence_score"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("query_importance", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "query_importance")
