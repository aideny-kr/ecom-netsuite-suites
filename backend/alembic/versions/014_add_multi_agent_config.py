"""Add multi_agent_enabled to tenant_configs.

Revision ID: 014
Revises: 013_chat_workspace_scope
"""

from alembic import op
import sqlalchemy as sa

revision = "014_add_multi_agent_config"
down_revision = "013_chat_workspace_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_configs",
        sa.Column(
            "multi_agent_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "multi_agent_enabled")
