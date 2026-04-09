"""Add source_pin column to chat_sessions for v0.1 source picker."""

import sqlalchemy as sa

from alembic import op

revision = "064_source_pin"
down_revision = "063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("source_pin", sa.String(16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("chat_sessions", "source_pin")
