"""Add agent_id to chat_messages for per-message agent tracking."""

import sqlalchemy as sa

from alembic import op

revision = "063"
down_revision = "062_recon_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("agent_id", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_messages", "agent_id")
