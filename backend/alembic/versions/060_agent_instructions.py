"""Add user_instructions columns to agent_configs."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "060_agent_instr"
down_revision = "059_task_files"

branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agent_configs", sa.Column("user_instructions", sa.Text, nullable=True))
    op.add_column("agent_configs", sa.Column("instructions_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("agent_configs", sa.Column("instructions_updated_by", UUID(as_uuid=True), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_configs", "instructions_updated_by")
    op.drop_column("agent_configs", "instructions_updated_at")
    op.drop_column("agent_configs", "user_instructions")
