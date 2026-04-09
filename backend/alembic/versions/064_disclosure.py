"""Add disclosure fields: chat_sessions.source_pin and chat_messages.disclosure_json."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "064_disclosure"
down_revision = "063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("source_pin", sa.String(16), nullable=True),
    )
    op.add_column(
        "chat_messages",
        sa.Column("disclosure_json", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_messages", "disclosure_json")
    op.drop_column("chat_sessions", "source_pin")
