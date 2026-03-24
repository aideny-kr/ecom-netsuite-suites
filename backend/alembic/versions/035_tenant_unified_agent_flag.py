"""Add unified_agent_enabled to tenant_configs."""

import sqlalchemy as sa

from alembic import op

revision = "035_tenant_unified_agent_flag"
down_revision = "034_tenant_query_patterns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenant_configs",
        sa.Column("unified_agent_enabled", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("tenant_configs", "unified_agent_enabled")
