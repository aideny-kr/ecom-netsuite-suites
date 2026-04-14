"""Drop source_pin column from chat_sessions.

source_pick is now per-turn (transient), not persisted on the session.
"""

import sqlalchemy as sa

from alembic import op

revision = "067_drop_source_pin"
down_revision = "066_bench_vs_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("chat_sessions", "source_pin")


def downgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column("source_pin", sa.String(16), nullable=True),
    )
