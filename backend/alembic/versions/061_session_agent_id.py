"""Add agent_id to chat_sessions for tracking which specialized agent was used."""

from alembic import op
import sqlalchemy as sa

revision = "061_session_agent"
down_revision = "060_agent_instr"

def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("agent_id", sa.String(100), nullable=True))
    op.create_index("ix_chat_sessions_agent_id", "chat_sessions", ["agent_id"])

def downgrade() -> None:
    op.drop_index("ix_chat_sessions_agent_id", table_name="chat_sessions")
    op.drop_column("chat_sessions", "agent_id")
